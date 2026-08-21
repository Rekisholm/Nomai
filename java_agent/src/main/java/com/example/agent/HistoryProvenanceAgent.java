package com.example.agent;

import java.io.File;
import java.nio.file.Files;
import java.io.FileOutputStream;
import java.io.PrintStream;
import java.lang.instrument.ClassFileTransformer;
import java.lang.instrument.Instrumentation;
import java.lang.management.ManagementFactory;
import java.lang.reflect.Field;
import java.nio.charset.StandardCharsets;
import java.security.ProtectionDomain;
import java.sql.*;
import java.util.*;
import java.util.concurrent.atomic.AtomicLong;
import java.io.IOException;

import javassist.*;

import java.lang.reflect.InvocationHandler;
import java.lang.reflect.Method;
import java.lang.reflect.Proxy;
import java.time.OffsetDateTime;

public class HistoryProvenanceAgent {

    private static final String SNAPSHOT_XMIN_ALIAS = "__nomai_xmin";
    private static final String SNAPSHOT_XMAX_ALIAS = "__nomai_xmax";

    // Thread-local request context
    private static final ThreadLocal<String> REQUEST_ID_HOLDER = new ThreadLocal<>();
    private static final ThreadLocal<Integer> INVOKE_DEPTH = ThreadLocal.withInitial(() -> 0);
    private static final ThreadLocal<Integer> READ_SEQUENCE = ThreadLocal.withInitial(() -> 0);

    // Prepared statement metadata mapping (Statement -> removed column aliases); WeakHashMap avoids memory leaks
    private static final ThreadLocal<Map<Object, Set<String>>> PREPARE_META_HOLDER = ThreadLocal.withInitial(WeakHashMap::new);
    // Also keep the original metaJson for parsing table/pk information
    private static final ThreadLocal<Map<Object, String>> PREPARE_META_JSON_HOLDER = ThreadLocal.withInitial(WeakHashMap::new);

    // Temporarily store metaJson from the prepare phase because it must be bound to PreparedStatement after the method returns
    private static final ThreadLocal<String> LAST_PREPARE_META = new ThreadLocal<>();

    // Request ID counter
    private static final AtomicLong REQUEST_COUNTER = new AtomicLong(0);
    private static final String PROCESS_ID = getProcessId();

    // Log output stream
    public static final PrintStream LOG;
    public static final PrintStream READ_LOG;
    public static final PrintStream CONTEXT_LOG;
    static {
        try {
            File logDir = new File("/tmp/logs");
            if (!logDir.exists()) {
                logDir.mkdirs();
            }
            LOG = new PrintStream(new FileOutputStream(new File(logDir, "agent.log"), true));
            READ_LOG = new PrintStream(new FileOutputStream(new File(logDir, "read_provenance.log"), true));
            CONTEXT_LOG = new PrintStream(new FileOutputStream(new File(logDir, "request_context.log"), true));

            File configFile = new File("/tmp/table_config.json");
            String tableConfig = "[]";
            try {
                tableConfig = String.join("", Files.readAllLines(configFile.toPath()));
            } catch (IOException e) {
                e.printStackTrace();
            }

            try {
                SqlRewriteJni.initTableConfig(tableConfig);
                LOG.println("[Agent] Init table config success");
                LOG.println("[Agent] Table config: " + tableConfig);
            } catch (RuntimeException e) {
                LOG.println("[Agent] Init table config failed: " + e.getMessage());
                throw e;
            }

        } catch (IOException e) {
            throw new RuntimeException("Failed to create log file", e);
        }
    }

    public static void premain(String agentArgs, Instrumentation inst) {
        LOG.println("[Agent] Agent starting...");
        inst.addTransformer(new HpTransformer(), true);
        LOG.println("[Agent] Agent started successfully");
    }

    public static String beginRequest() {
        // 1. Increase reference count
        int depth = INVOKE_DEPTH.get() + 1;
        INVOKE_DEPTH.set(depth);

        // 2. Get the current ID and generate one if empty, meaning this is the first entry at the outermost layer
        String currentId = REQUEST_ID_HOLDER.get();
        if (currentId == null) {
            currentId = generateRequestId();
            REQUEST_ID_HOLDER.set(currentId);
            READ_SEQUENCE.set(0);
            // Log the newly generated ID here if needed
        }
        
        return currentId;
    }

    public static void endRequest() {
        // 1. Decrease reference count
        int depth = INVOKE_DEPTH.get() - 1;

        if (depth <= 0) {
            // 2. Only clean up resources when returning to the outermost layer (depth <= 0)
            INVOKE_DEPTH.remove();
            REQUEST_ID_HOLDER.remove();
            READ_SEQUENCE.remove();
            // Clean up other ThreadLocal resources
            clearPrepareMetaHolder(); 
        } else {
            // 3. Otherwise only update the count and do not clean up data
            INVOKE_DEPTH.set(depth);
        }
    }
    // Generate a globally unique Request-Id: pid-threadId-counter
    private static String generateRequestId() {
        long threadId = Thread.currentThread().getId();
        long c = REQUEST_COUNTER.incrementAndGet();
        return PROCESS_ID + "-" + threadId + "-" + c;
    }

    public static void setCurrentRequestId(String id) {
        REQUEST_ID_HOLDER.set(id);
        READ_SEQUENCE.set(0);
    }

    public static String getCurrentRequestId() {
        return REQUEST_ID_HOLDER.get();
    }

    public static void clearCurrentRequestId() {
        REQUEST_ID_HOLDER.remove();
        READ_SEQUENCE.remove();
    }

    public static void clearPrepareMetaHolder() {
        PREPARE_META_HOLDER.remove();
        PREPARE_META_JSON_HOLDER.remove();
    }

    public static void setLastPrepareMeta(String metaJson) {
        LAST_PREPARE_META.set(metaJson);
    }

    public static String getLastPrepareMeta() {
        return LAST_PREPARE_META.get();
    }

    public static void removeLastPrepareMeta() {
        LAST_PREPARE_META.remove();
    }

    public static int nextReadSequence() {
        int sequence = READ_SEQUENCE.get();
        READ_SEQUENCE.set(sequence + 1);
        return sequence;
    }

    private static String getProcessId() {
        String name = ManagementFactory.getRuntimeMXBean().getName();
        return name.split("@")[0];
    }

    // ==========================================
    // Priority-v2 request context collection
    // ==========================================
    // Called in the finally block of Spring DispatcherServlet.doService.
    // At this point authentication is complete and routing has matched, so user_id and api_id can be extracted from HttpServletRequest.
    // Collection failures must fail open and must not affect application responses.
    // Because the Agent does not depend on the Servlet API at compile time, everything here uses reflection.

    public static void recordRequestContext(Object reqObj) {
        String rid = REQUEST_ID_HOLDER.get();
        if (rid == null || reqObj == null) {
            return;
        }
        try {
            Class<?> rc = reqObj.getClass();

            // --- method ---
            String method = "GET";
            try {
                Object m = rc.getMethod("getMethod").invoke(reqObj);
                if (m != null) method = ((String) m).toUpperCase(Locale.ROOT);
            } catch (Throwable t) {}

            // --- route pattern (Spring bestMatchingPattern) ---
            String route = null;
            try {
                Object pattern = rc.getMethod("getAttribute", String.class)
                        .invoke(reqObj, "org.springframework.web.servlet.HandlerMapping.bestMatchingPattern");
                if (pattern != null) {
                    route = pattern.toString();
                }
            } catch (Throwable t) {}
            if (route == null || route.isEmpty()) {
                try {
                    route = (String) rc.getMethod("getServletPath").invoke(reqObj);
                } catch (Throwable t) {}
            }
            if (route == null || route.isEmpty()) {
                try {
                    route = (String) rc.getMethod("getRequestURI").invoke(reqObj);
                } catch (Throwable t) {}
            }
            if (route == null) route = "/";
            String apiId = method + " " + route;

            // --- user identity ---
            String userId = null;
            String identitySource = "unknown";

            // 1. Servlet Principal
            if (userId == null) {
                try {
                    Object principal = rc.getMethod("getUserPrincipal").invoke(reqObj);
                    if (principal != null) {
                        String name = (String) principal.getClass().getMethod("getName").invoke(principal);
                        if (name != null && !name.isEmpty() && !"anonymousUser".equals(name)) {
                            userId = "user:" + name;
                            identitySource = "servlet_principal";
                        }
                    }
                } catch (Throwable t) {}
            }

            // 2. Request attributes (DS LoginHandlerInterceptor stores "session.user")
            if (userId == null) {
                userId = extractUserIdFromRequestAttributes(reqObj, rc);
                if (userId != null) identitySource = "request_attribute";
            }

            // 3. Session attributes (fallback)
            if (userId == null) {
                userId = extractUserIdFromSession(reqObj, rc);
                if (userId != null) identitySource = "session_attribute";
            }

            // 4. Session cookie hash fallback
            if (userId == null) {
                String cookieVal = extractSessionCookieValue(reqObj, rc);
                if (cookieVal != null) {
                    String digest = sha256Short(cookieVal);
                    userId = "session:" + digest;
                    identitySource = "session_cookie_sha256";
                }
            }

            // 4. Final fallback: request:<rid> (degrades to v1)
            if (userId == null) {
                userId = "request:" + rid;
                identitySource = "request_id";
            }

            // --- write JSONL ---
            String nowStr = OffsetDateTime.now().toString();
            StringBuilder sb = new StringBuilder(256);
            sb.append("{\"time\":\"").append(escapeJson(nowStr)).append("\"");
            sb.append(",\"rid\":\"").append(escapeJson(rid)).append("\"");
            sb.append(",\"method\":\"").append(escapeJson(method)).append("\"");
            sb.append(",\"route\":\"").append(escapeJson(route)).append("\"");
            sb.append(",\"api_id\":\"").append(escapeJson(apiId)).append("\"");
            sb.append(",\"user_id\":\"").append(escapeJson(userId)).append("\"");
            sb.append(",\"identity_source\":\"").append(escapeJson(identitySource)).append("\"");
            sb.append("}");
            CONTEXT_LOG.println(sb.toString());

        } catch (Throwable t) {
            LOG.println("[RequestContext] error: " + t.getMessage());
        }
    }

    /**
     * Extract user ID from HttpServletRequest attributes.
     * DS LoginHandlerInterceptor calls request.setAttribute("session.user", user) in preHandle.
     */
    private static String extractUserIdFromRequestAttributes(Object reqObj, Class<?> rc) {
        try {
            java.lang.reflect.Method getAttribute = rc.getMethod("getAttribute", String.class);
            // DS specific: "session.user" -> User -> getId()
            String[] userAttrNames = {"session.user", "user", "loginUser", "currentUser"};
            for (String attrName : userAttrNames) {
                try {
                    Object userObj = getAttribute.invoke(reqObj, attrName);
                    if (userObj == null) continue;
                    String id = tryGetUserIdFromObject(userObj);
                    if (id != null) {
                        return "user:" + id;
                    }
                } catch (Throwable t) {}
            }
            return null;
        } catch (Throwable t) {
            return null;
        }
    }

    /**
     * Extract user ID from HttpSession. Use reflection to avoid compile-time dependency on the Servlet API.
     * DS 3.1.8 SessionAuthenticator stores LoginUser in the "session.user" session attribute.
     */
    @SuppressWarnings("unchecked")
    private static String extractUserIdFromSession(Object reqObj, Class<?> rc) {
        try {
            // Get the session without creating a new one
            Object session = null;
            try {
                session = rc.getMethod("getSession", boolean.class).invoke(reqObj, Boolean.FALSE);
            } catch (NoSuchMethodException e) {
                session = rc.getMethod("getSession").invoke(reqObj);
            }
            if (session == null) return null;

            Class<?> sessionClass = session.getClass();
            java.lang.reflect.Method getAttribute = sessionClass.getMethod("getAttribute", String.class);

            // DS specific: "session.user" -> LoginUser -> getId()
            String[] userAttrNames = {"session.user", "user", "loginUser", "currentUser", "userInfo", "sessionUser"};
            for (String attrName : userAttrNames) {
                try {
                    Object userObj = getAttribute.invoke(session, attrName);
                    if (userObj == null) continue;
                    String id = tryGetUserIdFromObject(userObj);
                    if (id != null) {
                        return "user:" + id;
                    }
                } catch (Throwable t) {}
            }

            // Fallback: scan all session attributes for keys containing "user"
            try {
                java.lang.reflect.Method getAttributeNames = sessionClass.getMethod("getAttributeNames");
                java.util.Enumeration<String> names = (java.util.Enumeration<String>) getAttributeNames.invoke(session);
                while (names != null && names.hasMoreElements()) {
                    String attrName = names.nextElement();
                    if (attrName == null) continue;
                    String lowerName = attrName.toLowerCase(Locale.ROOT);
                    if (lowerName.contains("user") || lowerName.contains("login")) {
                        try {
                            Object userObj = getAttribute.invoke(session, attrName);
                            if (userObj == null) continue;
                            String id = tryGetUserIdFromObject(userObj);
                            if (id != null) {
                                LOG.println("[RequestContext] Found user in session attr: " + attrName
                                        + " class=" + userObj.getClass().getName());
                                return "user:" + id;
                            }
                        } catch (Throwable t) {}
                    }
                }
            } catch (Throwable t) {}

            return null;
        } catch (Throwable t) {
            return null;
        }
    }

    /**
     * Try to extract user ID from an object: getId() / getUserId() / getUsrId() / getValue("id")
     */
    private static String tryGetUserIdFromObject(Object obj) {
        String[] getters = {"getId", "getUserId", "getUsrId"};
        for (String getter : getters) {
            try {
                java.lang.reflect.Method m = obj.getClass().getMethod(getter);
                Object result = m.invoke(obj);
                if (result != null) {
                    String str = String.valueOf(result);
                    if (!str.isEmpty() && !"0".equals(str) && !"null".equals(str)) {
                        return str;
                    }
                }
            } catch (Throwable t) {}
        }
        return null;
    }

    /**
     * Extract the session cookie value from request cookies for anonymous identity digest
     */
    private static String extractSessionCookieValue(Object reqObj, Class<?> rc) {
        try {
            Object cookies = rc.getMethod("getCookies").invoke(reqObj);
            if (cookies == null) return null;
            Object[] cookieArr = (Object[]) cookies;
            for (Object cookie : cookieArr) {
                try {
                    String name = (String) cookie.getClass().getMethod("getName").invoke(cookie);
                    String value = (String) cookie.getClass().getMethod("getValue").invoke(cookie);
                    if (name != null && value != null && !value.isEmpty()) {
                        // Prefer JSESSIONID or DS custom sessionId cookie
                        if ("JSESSIONID".equals(name) || name.toLowerCase(Locale.ROOT).contains("session")) {
                            return value;
                        }
                    }
                } catch (Throwable t) {}
            }
            // Fallback: use the first non-empty cookie
            for (Object cookie : cookieArr) {
                try {
                    String value = (String) cookie.getClass().getMethod("getValue").invoke(cookie);
                    if (value != null && !value.isEmpty() && value.length() > 8) {
                        return value;
                    }
                } catch (Throwable t) {}
            }
        } catch (Throwable t) {}
        return null;
    }

    /**
     * SHA-256 digest, using the first 24 hexadecimal characters
     */
    private static String sha256Short(String input) {
        try {
            java.security.MessageDigest md = java.security.MessageDigest.getInstance("SHA-256");
            byte[] hash = md.digest(input.getBytes(java.nio.charset.StandardCharsets.UTF_8));
            StringBuilder hex = new StringBuilder(64);
            for (byte b : hash) {
                hex.append(String.format("%02x", b));
            }
            return hex.substring(0, 24);
        } catch (Throwable t) {
            return Integer.toHexString(input.hashCode());
        }
    }

    // Parse Meta JSON returned by Rust and extract column aliases to remove
    public static Set<String> parseRemoveColumns(String metaJson) {
        Set<String> removeCols = new HashSet<>();
        if (metaJson == null || metaJson.isEmpty()) {
            return removeCols;
        }

        try {
            // Simple JSON parsing; Jackson/Gson is recommended for production
            String cleanMeta = metaJson.replace("[", "").replace("]", "").trim();
            if (cleanMeta.isEmpty()) {
                return removeCols;
            }

            String[] items = cleanMeta.split("},");
            for (String item : items) {
                String trimmedItem = item.replace("{", "").replace("}", "").replace("\"", "").trim();
                String[] kvPairs = trimmedItem.split(",");
                for (String kv : kvPairs) {
                    String[] kvArr = kv.split(":", 2);
                    if (kvArr.length == 2 && "alias_used".equals(kvArr[0].trim())) {
                        removeCols.add(normalizeColumnLabel(kvArr[1].trim()));
                        break;
                    }
                }
            }
        } catch (Exception e) {
            LOG.println("[parseRemoveColumns] Failed to parse meta: " + metaJson + ", error: " + e.getMessage());
        }
        return removeCols;
    }

    // Parse metaJson more completely and return table/pk_col/alias_used for each entry for logging
    public static List<Map<String, String>> parseMetaItems(String metaJson) {
        List<Map<String, String>> out = new ArrayList<>();
        if (metaJson == null || metaJson.isEmpty()) return out;
        try {
            String cleanMeta = metaJson.trim();
            if (cleanMeta.startsWith("[")) {
                cleanMeta = cleanMeta.substring(1);
            }
            if (cleanMeta.endsWith("]")) {
                cleanMeta = cleanMeta.substring(0, cleanMeta.length()-1);
            }
            if (cleanMeta.trim().isEmpty()) return out;

            String[] items = cleanMeta.split("},");
            for (String item : items) {
                String s = item.trim();
                if (!s.endsWith("}")) s = s + "}";
                s = s.replace("{", "").replace("}", "").replace("\"", "").trim();
                String[] kvPairs = s.split(",");
                Map<String, String> map = new HashMap<>();
                for (String kv : kvPairs) {
                    String[] kvArr = kv.split(":", 2);
                    if (kvArr.length == 2) {
                        map.put(kvArr[0].trim(), kvArr[1].trim());
                    }
                }
                if (!map.isEmpty()) out.add(map);
            }
        } catch (Exception e) {
            LOG.println("[parseMetaItems] Failed to parse metaJson: " + metaJson + ", error: " + e.getMessage());
        }
        return out;
    }

    /*
    Utility method: determine whether SQL is a write operation (INSERT/UPDATE/DELETE)
    */
    public static boolean isWriteOperation(String sql) {
        if (sql == null || sql.isEmpty()) return false;
        // 1. Convert to uppercase for logic checks only; do not change the original SQL
        String upper = sql.toUpperCase();
        // 2. Check whether key write-operation keywords exist
        // Use contains to mimic Python .in.
        return upper.contains("INSERT") || upper.contains("UPDATE") || upper.contains("DELETE");
    }

    /**
     * Utility method: safely get final SQL from PgPreparedStatement, compatible with different JDBC versions
     */
    public static String getFinalSql(Object statement) {
        if (statement == null) {
            return "UNKNOWN_SQL";
        }
        // Prefer toString(), because PgPreparedStatement.toString outputs SQL with parameters
        String sql = statement.toString();
        if (sql != null && !sql.isEmpty()) {
            int sqlindex = sql.indexOf("::");
            int sqlStart = (sqlindex < 0 ? 0 : sqlindex + 2);
            int sqlEnd = sql.indexOf("[") > 0 ? sql.indexOf("[") : sql.length();
            if (sqlStart > 0 && sqlEnd > sqlStart) {
                sql = sql.substring(sqlStart, sqlEnd).trim();
            }
        }
        // Reflection fallback: get private field originalSql for older JDBC versions
        if ("UNKNOWN_SQL".equals(sql)) {
            try {
                Field sqlField = statement.getClass().getDeclaredField("originalSql");
                sqlField.setAccessible(true);
                sql = (String) sqlField.get(statement);
            } catch (Exception e) {
                try {
                    // Alternative private field name for different JDBC versions
                    Field sqlField = statement.getClass().getDeclaredField("sql");
                    sqlField.setAccessible(true);
                    sql = (String) sqlField.get(statement);
                } catch (Exception ex) {
                    LOG.println("[getFinalSql] Failed to get SQL via reflection: " + ex.getMessage());
                }
            }
        }
        return sql == null ? "UNKNOWN_SQL" : sql;
    }

    /**
     * Bind PreparedStatement metadata, called from the after hook in transformPgConnection
     * Receive stmt as Object to avoid classloader conflicts
     */
    public static void setPrepareMeta(Object stmt, String metaJson) {
        if (stmt == null || metaJson == null || metaJson.isEmpty()) return;
        try {
            Set<String> removeCols = parseRemoveColumns(metaJson);
            PREPARE_META_HOLDER.get().put(stmt, removeCols);
            PREPARE_META_JSON_HOLDER.get().put(stmt, metaJson);
            //LOG.println("[Agent] setPrepareMeta stmt=" + stmt.hashCode() + " removeCols=" + removeCols);
        } catch (Throwable t) {
            LOG.println("[Agent] setPrepareMeta error: " + t.getMessage());
        } finally {
            LAST_PREPARE_META.remove();
        }
    }

    /**
     * After query execution, record read dependencies and return a wrapped ResultSet that hides injected columns
     * Called from injected bytecode, and the return value replaces the original ResultSet via $_ = wrapAndRecordRead(...)
     */
    public static java.sql.ResultSet wrapAndRecordRead(String reqId, java.sql.ResultSet rs, Object stmt) {
        if (rs == null) return null;
        try {
            // Get this Statement.s metaJson and columns to remove
            String metaJson = PREPARE_META_JSON_HOLDER.get().get(stmt);
            Set<String> removeCols = PREPARE_META_HOLDER.get().get(stmt);

            if ((metaJson == null || metaJson.isEmpty()) && (removeCols == null || removeCols.isEmpty())) {
                LOG.println("[Debug-Miss] Proxy NOT created for reqId: " + reqId + ", stmt: " + stmt.getClass().getName());
                return rs;
            }
            //else{
            //    LOG.println("[Debug-Hit] Proxy created for reqId: " + reqId + ", stmt: " + stmt.getClass().getName());
            //}
            if (removeCols == null || removeCols.isEmpty()) {
                return rs;
            }

            // 1. Parse metaJson and build an alias -> table mapping for later logging
            List<Map<String, String>> metaItems = parseMetaItems(metaJson);
            Map<String, String> aliasToTable = new HashMap<>();
            for (Map<String, String> it : metaItems) {
                String alias = it.get("alias_used");
                String table = it.get("table");
                if (alias != null && table != null) {
                    aliasToTable.put(normalizeColumnLabel(alias), table);
                }
            }

            // 2. Create a proxy and encapsulate the read-and-record logic inside it
            // Pass context such as reqId and aliasToTable
            return createFilteredResultSetProxy(
                    rs, removeCols, aliasToTable, reqId, nextReadSequence()
            );

        } catch (Throwable tOuter) {
            LOG.println("[Agent] wrapAndRecordRead outer error: " + tOuter.getMessage());
            return rs;
        }
    }

    /**
     * Create a proxy ResultSet: hide columns (aliases) in removeCols and remap indexes for column-index-based methods.
     * This lets the upper ORM see the restored column set when calling getMetaData(), getObject(i), getString(i), and similar methods.
     */
    @SuppressWarnings("unchecked")
    private static java.sql.ResultSet createFilteredResultSetProxy(final java.sql.ResultSet rs,
                                                                   final Set<String> removeCols,
                                                                   final Map<String, String> aliasToTable,
                                                                   final String reqId,
                                                                   final int readSeq) {
        if (removeCols == null || removeCols.isEmpty()) {
            return rs;
        }
        try {
            final ResultSetMetaData md = rs.getMetaData();
            final int origCount = md.getColumnCount();

            // 1. Compute index mappings
            // keptIndexes: virtual index (0-based) -> real index (1-based)
            final List<Integer> keptIndexes = new ArrayList<>();
            final List<String> keptLabels = new ArrayList<>();
            // auditCols: real index (1-based) -> table name (for auditing)
            final Map<Integer, String> auditCols = new HashMap<>();
            final Set<String> hiddenCols = new HashSet<>(removeCols);
            hiddenCols.add(SNAPSHOT_XMIN_ALIAS);
            hiddenCols.add(SNAPSHOT_XMAX_ALIAS);
            int snapshotXminIndex = -1;
            int snapshotXmaxIndex = -1;

            for (int i = 1; i <= origCount; i++) {
                String lbl = md.getColumnLabel(i);
                if (lbl == null) lbl = md.getColumnName(i);
                if (lbl == null) lbl = "";
                String normalizedLabel = normalizeColumnLabel(lbl);

                if (SNAPSHOT_XMIN_ALIAS.equals(normalizedLabel)) {
                    snapshotXminIndex = i;
                }
                if (SNAPSHOT_XMAX_ALIAS.equals(normalizedLabel)) {
                    snapshotXmaxIndex = i;
                }

                if (hiddenCols.contains(normalizedLabel)) {
                    // Record hidden columns that need auditing
                    if (removeCols.contains(normalizedLabel)) {
                        String tableName = aliasToTable.get(normalizedLabel);
                        auditCols.put(i, tableName != null ? tableName : "unknown");
                    }
                } else {
                    // Record kept columns
                    keptIndexes.add(i);
                    keptLabels.add(lbl);
                }
            }

            if (keptIndexes.size() == origCount) {
                return rs;
            }

            final int snapshotXminRealIndex = snapshotXminIndex;
            final int snapshotXmaxRealIndex = snapshotXmaxIndex;
            final Long[] seenXmin = new Long[]{null};
            final Long[] seenXmax = new Long[]{null};

            // 2. Proxy ResultSetMetaData
            Object metaProxy = Proxy.newProxyInstance(
                    HistoryProvenanceAgent.class.getClassLoader(),
                    new Class[]{ResultSetMetaData.class},
                    new InvocationHandler() {
                        @Override
                        public Object invoke(Object proxy, Method method, Object[] args) throws Throwable {
                            String name = method.getName();
                            if ("getColumnCount".equals(name)) {
                                return keptIndexes.size();
                            }
                            // Handle index-based methods (getXxx(int column), isXxx(int column), etc.)
                            if (args != null && args.length > 0 && args[0] instanceof Integer) {
                                int idx = (Integer) args[0];
                                if (idx < 1 || idx > keptIndexes.size()) {
                                    throw new SQLException("Column index out of range: " + idx);
                                }
                                args[0] = keptIndexes.get(idx - 1); // Remap to real index
                            }
                            return method.invoke(md, args);
                        }
                    }
            );

            // 3. Proxy ResultSet
            Object rsProxy = Proxy.newProxyInstance(
                    HistoryProvenanceAgent.class.getClassLoader(),
                    new Class[]{ResultSet.class},
                    new InvocationHandler() {
                        @Override
                        public Object invoke(Object proxy, Method method, Object[] args) throws Throwable {
                            String name = method.getName();

                            // --- A. Intercept next() for auditing ---
                            if ("next".equals(name) && (args == null || args.length == 0)) {
                                boolean hasNext = rs.next();
                                if (hasNext && !auditCols.isEmpty()) {
                                    try {
                                        String nowStr = OffsetDateTime.now().toString();
                                        Long xmin = null;
                                        Long xmax = null;
                                        if (snapshotXminRealIndex > 0 && snapshotXmaxRealIndex > 0) {
                                            xmin = readLongColumn(rs, snapshotXminRealIndex);
                                            xmax = readLongColumn(rs, snapshotXmaxRealIndex);
                                            if (xmin != null && xmax != null) {
                                                if (seenXmin[0] == null && seenXmax[0] == null) {
                                                    seenXmin[0] = xmin;
                                                    seenXmax[0] = xmax;
                                                } else if (!seenXmin[0].equals(xmin) || !seenXmax[0].equals(xmax)) {
                                                    throw new SQLException(
                                                        "inconsistent snapshot bounds: xmin="
                                                        + seenXmin[0] + "/" + xmin
                                                        + ", xmax=" + seenXmax[0] + "/" + xmax
                                                    );
                                                }
                                            }
                                        }
                                        for (Map.Entry<Integer, String> entry : auditCols.entrySet()) {
                                            int realIdx = entry.getKey();
                                            String tbn = entry.getValue();
                                            Object pkVal = rs.getObject(realIdx);
                                            if (pkVal == null) {
                                                continue;
                                            }
                                            StringBuilder line = new StringBuilder();
                                            line.append("{\"time\":\"").append(escapeJson(nowStr))
                                                .append("\",\"event\":\"select\",\"rid\":\"")
                                                .append(escapeJson(reqId))
                                                .append("\",\"tbn\":\"")
                                                .append(escapeJson(tbn))
                                                .append("\",\"pks\":[\"")
                                                .append(escapeJson(String.valueOf(pkVal)))
                                                .append("\"]");
                                            if (xmin != null && xmax != null) {
                                                line.append(",\"seq\":").append(readSeq)
                                                    .append(",\"xmin\":").append(xmin)
                                                    .append(",\"xmax\":").append(xmax);
                                            }
                                            line.append("}");
                                            READ_LOG.println(line.toString());
                                        }
                                    } catch (Throwable t) {
                                        // Swallow audit exceptions to avoid affecting business logic
                                        LOG.println("[AuditError] " + t.getMessage());
                                    }
                                }
                                return hasNext;
                            }

                            // --- B. Intercept getMetaData ---
                            if ("getMetaData".equals(name)) {
                                return metaProxy;
                            }

                            // --- C. Intercept findColumn (reverse mapping) ---
                            // The upper layer asks which column User is; the lower layer returns column 10. We must correct it to column 5, assuming only 5 visible columns remain.
                            if ("findColumn".equals(name) && args != null && args.length == 1 && args[0] instanceof String) {
                                String label = (String) args[0];
                                if (hiddenCols.contains(normalizeColumnLabel(label))) {
                                    throw new SQLException("Column '" + label + "' not found (hidden by agent).");
                                }
                                // Get the real index
                                int realIdx = rs.findColumn(label);
                                // Reverse lookup: position of the real index in keptIndexes
                                int virtualIdx = keptIndexes.indexOf(realIdx);
                                if (virtualIdx == -1) {
                                    // Theoretically, if rs.findColumn succeeds and the column is not in removeCols, this should not fail
                                    // But for safety:
                                    throw new SQLException("Column '" + label + "' found in DB but not in mapped view.");
                                }
                                // Convert to 1-based virtual index
                                return virtualIdx + 1;
                            }

                            // --- D. Intercept column-index-based get/update methods ---
                            // Core fix: intercept only explicit column operation methods to avoid incorrectly intercepting absolute(row), setFetchSize(rows), etc.
                            if (args != null && args.length >= 1 && args[0] instanceof Integer) {
                                // Determine whether this is a column operation method: starts with get/update and is not a known non-column method
                                if (isColumnIndexMethod(name)) {
                                    int idx = (Integer) args[0];
                                    if (idx < 1 || idx > keptIndexes.size()) {
                                        throw new SQLException("Invalid column index: " + idx);
                                    }
                                    // Map to the real index
                                    args[0] = keptIndexes.get(idx - 1);
                                    return method.invoke(rs, args);
                                }
                            }

                            // --- E. Intercept column-name-based get/update methods (hidden-column protection) ---
                            if (args != null && args.length >= 1 && args[0] instanceof String) {
                                // Method-name check: avoid accidentally intercepting setCursorName(String) and similar methods; usually low risk, but keep behavior consistent
                                if (name.startsWith("get") || name.startsWith("update")) {
                                    String label = (String) args[0];
                                    if (hiddenCols.contains(normalizeColumnLabel(label))) {
                                        throw new SQLException("Column '" + label + "' is hidden.");
                                    }
                                }
                            }

                            // Pass other methods through directly (close, isClosed, wasNull, absolute, etc.)
                            return method.invoke(rs, args);
                        }
                    }
            );

            return (ResultSet) rsProxy;

        } catch (Throwable t) {
            LOG.println("[Agent] createFilteredResultSetProxy error: " + t.getMessage());
            t.printStackTrace(LOG);
            return rs;
        }
    }

    private static String normalizeColumnLabel(String label) {
        return label == null ? "" : label.toLowerCase(Locale.ROOT);
    }

    private static Long readLongColumn(ResultSet rs, int columnIndex) throws SQLException {
        Object value = rs.getObject(columnIndex);
        if (value == null) {
            return null;
        }
        if (value instanceof Number) {
            return ((Number) value).longValue();
        }
        try {
            return Long.parseLong(String.valueOf(value));
        } catch (NumberFormatException e) {
            throw new SQLException("snapshot boundary is not numeric: " + value, e);
        }
    }

    private static String escapeJson(String value) {
        if (value == null) {
            return "";
        }
        StringBuilder out = new StringBuilder(value.length() + 16);
        for (int i = 0; i < value.length(); i++) {
            char ch = value.charAt(i);
            switch (ch) {
                case '\\':
                    out.append("\\\\");
                    break;
                case '"':
                    out.append("\\\"");
                    break;
                case '\n':
                    out.append("\\n");
                    break;
                case '\r':
                    out.append("\\r");
                    break;
                case '\t':
                    out.append("\\t");
                    break;
                default:
                    if (ch < 0x20) {
                        out.append(String.format("\\u%04x", (int) ch));
                    } else {
                        out.append(ch);
                    }
            }
        }
        return out.toString();
    }

    /**
     * Helper method: determine whether a method name is a column-index-based operation
     * Avoid affecting absolute(int row), setFetchSize(int rows), etc.
     */
    private static boolean isColumnIndexMethod(String name) {
        // Must start with get or update
        if (!name.startsWith("get") && !name.startsWith("update")) {
            return false;
        }
        // Exclude known methods that start with get but whose argument is not a column index
        // Note: this only checks the name; the call site also checks args[0] is Integer
        if ("getFetchSize".equals(name) ||
            "getMaxFieldSize".equals(name) ||
            "getQueryTimeout".equals(name) ||
            "getGeneratedKeys".equals(name) ||
            "getResultSetConcurrency".equals(name) ||
            "getResultSetType".equals(name) ||
            "getResultSetHoldability".equals(name)) {
            return false;
        }
        return true;
    }
    /**
     * Actual bytecode transformer
     */
    public static class HpTransformer implements ClassFileTransformer {

        @Override
        public byte[] transform(ClassLoader loader, String className,
                                Class<?> classBeingRedefined,
                                ProtectionDomain protectionDomain,
                                byte[] classfileBuffer) {

            if (className == null) {
                return null;
            }

            try {
                // 1. Intercept Tomcat request entry point to generate RequestId
                if (className.startsWith("org/apache/catalina/core/StandardWrapperValve")) {
                    return transformStandardWrapperValve(className, loader, classfileBuffer);
                }

                // 2. Intercept Spring DispatcherServlet to generate RequestId   
                if (className.equals("org/springframework/web/servlet/DispatcherServlet")) {
                    return transformSpringDispatcherServlet(className, loader, classfileBuffer);
                }

                // 3. Intercept PG Connection to rewrite SQL during prepare phase
                if (className.endsWith("PgConnection")) {
                    return transformPgConnection(className, loader, classfileBuffer);
                }

                // 4. Intercept PG Statement/PreparedStatement for execution-time injection and result-set processing
                if (className.startsWith("org/postgresql/jdbc/Pg")
                        && (className.endsWith("PgStatement")
                        || className.endsWith("PgPreparedStatement")
                        || className.endsWith("PgCallableStatement"))) {
                    return transformPgStatement(className, loader, classfileBuffer);
                }

            } catch (Throwable t) {
                LOG.println("[Agent] Transform error on " + className + ": " + t);
                t.printStackTrace(LOG);
            }
            return null;
        }

        /**
         * Intercept javax.servlet.http.HttpServlet.service(HttpServletRequest, HttpServletResponse)
         * Generate requestId for each request, store it in ThreadLocal, and write X-Request-Id to the response header
         */
        private byte[] transformStandardWrapperValve(String className, ClassLoader loader, byte[] classfileBuffer)
                throws Exception {

            ClassPool cp = ClassPool.getDefault();
            // Avoid missing dependencies under some classloaders
            if (loader != null) {
                cp.insertClassPath(new LoaderClassPath(loader));
            }

            CtClass ctClass = cp.makeClass(new java.io.ByteArrayInputStream(classfileBuffer));
            if (ctClass.isInterface()) {
                return null;
            }

            try {
                CtClass requestClass = cp.get("org.apache.catalina.connector.Request");
                CtClass responseClass = cp.get("org.apache.catalina.connector.Response");

                CtMethod invokeMethod = ctClass.getDeclaredMethod(
                    "invoke",
                    new CtClass[]{requestClass, responseClass}
                );

                LOG.println("[Agent] Instrumenting StandardWrapperValve.invoke in " + className);

                // Insert at method start: generate requestId and write ThreadLocal
                String beforeCode =
                    "{ " +
                    "  String _hpReqId = com.example.agent.HistoryProvenanceAgent.beginRequest();" +
                    "  try {" +
                    "    javax.servlet.http.HttpServletResponse _hpResp = (javax.servlet.http.HttpServletResponse)$2.getResponse();" +
                    "    if (_hpResp != null) {" +
                    "        _hpResp.setHeader(\"X-Request-Id\", _hpReqId);" +
                    "    }" +
                    "  } catch (Throwable _hpIgnore) {}" +
                    "}";

                invokeMethod.insertBefore(beforeCode);

                // Clean ThreadLocal in finally
                String afterCode =
                    "{ " +
                    "    com.example.agent.HistoryProvenanceAgent.endRequest();" +
                    "} ";

                invokeMethod.insertAfter(afterCode, true);

                byte[] byteCode = ctClass.toBytecode();
                ctClass.detach();
                return byteCode;
            } catch (NotFoundException e) {
                // If the specific method does not exist, the version is incompatible; ignore it
                ctClass.detach();
                return null;
            }
        }

        private byte[] transformSpringDispatcherServlet(String className, ClassLoader loader, byte[] classfileBuffer) throws Exception {
            ClassPool cp = ClassPool.getDefault();
            if (loader != null) {
                cp.insertClassPath(new LoaderClassPath(loader));
            }
            // System path may need to be added in Spring Boot environments to find javax.servlet
            cp.appendSystemPath();

            CtClass ctClass = cp.makeClass(new java.io.ByteArrayInputStream(classfileBuffer));
            
            try {
                // Core Spring MVC entry method:
                // protected void doService(HttpServletRequest request, HttpServletResponse response)
                CtClass reqClass = cp.get("javax.servlet.http.HttpServletRequest");
                CtClass respClass = cp.get("javax.servlet.http.HttpServletResponse");

                CtMethod doServiceMethod = ctClass.getDeclaredMethod("doService", new CtClass[]{reqClass, respClass});

                LOG.println("[Agent] Instrumenting DispatcherServlet (Strategy: Spring Boot) in " + className);

                // Injection logic: identical to the Tomcat version
                String beforeCode =
                    "{ " +
                    "  String _hpReqId = com.example.agent.HistoryProvenanceAgent.beginRequest();" +
                    "  try {" +
                    "    if ($2 != null && !$2.containsHeader(\"X-Request-Id\")) {" +
                    "        $2.setHeader(\"X-Request-Id\", _hpReqId);" +
                    "    }" +
                    "  } catch (Throwable _hpIgnore) {}" +
                    "}";

                doServiceMethod.insertBefore(beforeCode);

                String afterCode =
                    "{ " +
                    "    try {" +
                    "        com.example.agent.HistoryProvenanceAgent.recordRequestContext($1);" +
                    "    } catch (Throwable _hpCtx) {}" +
                    "    com.example.agent.HistoryProvenanceAgent.endRequest();" +
                    "} ";

                doServiceMethod.insertAfter(afterCode, true);

                byte[] byteCode = ctClass.toBytecode();
                ctClass.detach();
                return byteCode;

            } catch (Exception e) {
                LOG.println("[Agent] Failed to instrument DispatcherServlet: " + e.getMessage());
                ctClass.detach();
                return null;
            }
        }

        private byte[] transformPgConnection(String className, ClassLoader loader, byte[] classfileBuffer) throws Exception {
            ClassPool cp = createClassPool(loader);
            // Import required classes
            cp.importPackage("com.example.agent.SqlRewriteJni");
            cp.importPackage("java.util.Set");
            cp.importPackage("java.util.HashSet");

            CtClass ctClass = cp.makeClass(new java.io.ByteArrayInputStream(classfileBuffer));
            boolean modified = false;

            // Intercept all prepareStatement overloads whose first parameter is String
            for (CtMethod m : ctClass.getDeclaredMethods("prepareStatement")) {
                CtClass[] paramTypes = m.getParameterTypes();
                if (paramTypes.length >= 1 && "java.lang.String".equals(paramTypes[0].getName())) {
                    LOG.println("[HpTransformer] Instrumenting PgConnection.prepareStatement(" + paramTypes.length + " params)");

                    // Before execution: call Rust to rewrite SQL, but only if SQL has not already been rewritten
                    String beforeCode =
                            "{ " +
                                    "String _hpOriginalSql = $1;" +
                                    "String _hpReqId = com.example.agent.HistoryProvenanceAgent.getCurrentRequestId();" +
                                    "if (_hpOriginalSql != null  && _hpOriginalSql.indexOf(\"__pk_\") < 0 && _hpReqId != null) {" +
                                    "  try {" +
                                    "    com.example.agent.SqlRewriteJni.RewriteResult _hpRewriteRes = com.example.agent.SqlRewriteJni.safeRewriteSql(_hpOriginalSql);" +
                                    "    if (_hpRewriteRes != null && _hpRewriteRes.isSuccess() && _hpRewriteRes.newSql != null && !_hpRewriteRes.newSql.isEmpty()) {" +
                                    "       $1 = _hpRewriteRes.newSql;" +
                                    "       // Temporarily store metaJson and bind it to PreparedStatement after the method returns\n" +
                                    "       com.example.agent.HistoryProvenanceAgent.setLastPrepareMeta(_hpRewriteRes.metaJson);" +
                                    //"       com.example.agent.HistoryProvenanceAgent.LOG.println(\"[Rewrite] SQL rewritten: \" + _hpOriginalSql + \" -> \" + _hpRewriteRes.newSql);" +
                                    "    } else {" +
                                    "       com.example.agent.HistoryProvenanceAgent.LOG.println(\"[Rewrite] no rewrite applied for sql: \" + _hpOriginalSql);" +
                                    "    }" +
                                    "  } catch (Throwable _hpErr) {" +
                                    "    com.example.agent.HistoryProvenanceAgent.LOG.println(\"[Rewrite] error: \" + _hpErr.getMessage());" +
                                    "  }" +
                                    "}" +
                                    "}";

                    m.insertBefore(beforeCode);

                    // After execution: bind metadata to PreparedStatement if previously cached metadata exists
                    String afterCode =
                            "{ " +
                                    "    java.sql.PreparedStatement _hpStmt = ($_ instanceof java.sql.PreparedStatement) ? (java.sql.PreparedStatement)$_ : null;" +
                                    "    String _hpMeta = com.example.agent.HistoryProvenanceAgent.getLastPrepareMeta();" +
                                    "    if (_hpStmt != null && _hpMeta != null && !_hpMeta.isEmpty()) {" +
                                    "        try {" +
                                    "            com.example.agent.HistoryProvenanceAgent.setPrepareMeta(_hpStmt, _hpMeta);" +
                                    "        } catch (Throwable _hpIgnore) {" +
                                    "            com.example.agent.HistoryProvenanceAgent.LOG.println(\"[Prepare] setPrepareMeta error: \" + _hpIgnore.getMessage());" +
                                    "        } finally {" +
                                    "            com.example.agent.HistoryProvenanceAgent.removeLastPrepareMeta();" +
                                    "        }" +
                                    "    }" +
                                    "}";

                    m.insertAfter(afterCode);

                    modified = true;
                }
            }

            if (modified) {
                byte[] byteCode = ctClass.toBytecode();
                LOG.println("[HpTransformer] PgConnection instrumented successfully");
                ctClass.detach();
                return byteCode;
            } else {
                ctClass.detach();
                return null;
            }
        }


        //Enhance PG Statement: inject RequestId during execution and process result sets
        private byte[] transformPgStatement(String className, ClassLoader loader, byte[] classfileBuffer)
        throws Exception {

            ClassPool cp = ClassPool.getDefault();
            if (loader != null) {
                cp.insertClassPath(new LoaderClassPath(loader));
            }

            CtClass ctClass = cp.makeClass(new java.io.ByteArrayInputStream(classfileBuffer));
            if (ctClass.isInterface()) {
                ctClass.detach();
                return null;
            }

            boolean modified = false;

            // 1. Methods with SQL strings: executeQuery(String), executeUpdate(String), execute(String), addBatch(String)
            String[] stringSqlMethods = new String[]{
                "executeQuery",
                "executeUpdate",
                "execute",
                "addBatch"
            };

            for (String mname : stringSqlMethods) {
                for (CtMethod m : ctClass.getDeclaredMethods(mname)) {
                    CtClass[] paramTypes = m.getParameterTypes();
                    if (paramTypes.length == 1 && "java.lang.String".equals(paramTypes[0].getName())) {
                        LOG.println("[Agent] Instrumenting " + className + "." + mname + "(String)");

                        StringBuilder code = new StringBuilder();
                        code.append("{");
                        code.append("  String _hpSql = $1;\n");
                        // Prevent reinjecting into the SET request_id statement that we injected
                        code.append("  if (_hpSql != null && _hpSql.regionMatches(true, 0, \"SET\", 0, 3)) {");
                        code.append("    // Skip injection logic directly\n");
                        code.append("  } else {");
                        code.append("    String _hpReqId = com.example.agent.HistoryProvenanceAgent.getCurrentRequestId();");
                        code.append("    if (_hpReqId != null) {");

                        // SQL Rewrite Logic for RAW SQL ---
                        code.append("      try {");
                        code.append("        if (_hpSql.indexOf(\"__pk_\") < 0) {"); // Avoid duplicate injection
                        code.append("           com.example.agent.SqlRewriteJni.RewriteResult _hpRes = com.example.agent.SqlRewriteJni.safeRewriteSql(_hpSql);");
                        code.append("           if (_hpRes != null && _hpRes.isSuccess() && _hpRes.newSql != null && !_hpRes.newSql.isEmpty()) {");
                        code.append("             $1 = _hpRes.newSql;"); // Modify input SQL
                        code.append("             _hpSql = _hpRes.newSql;");
                        // Bind metadata to the current Statement (this)
                        code.append("             com.example.agent.HistoryProvenanceAgent.setPrepareMeta(this, _hpRes.metaJson);");
                        code.append("           }");
                        code.append("        }");
                        code.append("      } catch (Throwable _rwErr) {");
                        code.append("        com.example.agent.HistoryProvenanceAgent.LOG.println(\"[Rewrite-Raw-Error] \" + _rwErr.getMessage());");
                        code.append("      }");
                        // --- End Rewrite Logic ---

                        code.append("      com.example.agent.HistoryProvenanceAgent.LOG.println(\"[SQL-RAW] \" + _hpReqId + \" \" +  _hpSql );");
                        code.append("      try {");
                        code.append("        java.sql.Connection _hpConn = this.getConnection();");
                        code.append("        if (com.example.agent.HistoryProvenanceAgent.isWriteOperation(_hpSql)) {");
                        code.append("           if (_hpConn != null) {");
                        code.append("               java.sql.Statement _hpStmt = _hpConn.createStatement();");
                        code.append("               _hpStmt.execute(\"SET request_id = '\" + _hpReqId.replace(\"'\",\"''\") + \"'\");");
                        code.append("               _hpStmt.close();");
                        code.append("           }");
                        code.append("        }");
                        code.append("      } catch (Throwable _hpIgnore) {}");
                        code.append("    }");
                        code.append("  }");
                        code.append("}");

                        m.insertBefore(code.toString());

                        // --- [New] executeQuery(String) result-set processing ---
                        // For execute(String), users later call getResultSet(), which is already instrumented, so no extra handling is needed here
                        if ("executeQuery".equals(mname)) {
                            StringBuilder afterCode = new StringBuilder();
                            afterCode.append("{");
                            afterCode.append("  java.sql.ResultSet _hpRs = $_;\n");
                            afterCode.append("  String _hpReqId = com.example.agent.HistoryProvenanceAgent.getCurrentRequestId();");
                            afterCode.append("  try {");
                            afterCode.append("    if (_hpReqId != null && _hpRs != null) {");
                            // This checks metadata previously bound by setPrepareMeta
                            afterCode.append("      $_ = com.example.agent.HistoryProvenanceAgent.wrapAndRecordRead(_hpReqId, _hpRs, this);");
                            afterCode.append("    }");
                            afterCode.append("  } catch (Throwable _hpIgnore) {}");
                            afterCode.append("}");
                            m.insertAfter(afterCode.toString(), true);
                        }
                        modified = true;
                    }
                }
            }

            // 2. Methods without SQL strings: executeQuery(), executeUpdate(), execute(), mainly PreparedStatement
            String[] noArgMethods = new String[]{
                "executeQuery",
                "executeUpdate",
                "execute",
                "getResultSet" // <--- add this
            };

            for (String mname : noArgMethods) {
                for (CtMethod m : ctClass.getDeclaredMethods(mname)) {
                    CtClass[] paramTypes = m.getParameterTypes();
                    if (paramTypes.length == 0) {
                        LOG.println("[Agent] Instrumenting " + className + "." + mname + "()");
                        
                        if (!"getResultSet".equals(mname)) {
                            StringBuilder code = new StringBuilder();
                            code.append("{");
                            code.append("  String _hpReqId = com.example.agent.HistoryProvenanceAgent.getCurrentRequestId();");
                            code.append("  String _hpSql = com.example.agent.HistoryProvenanceAgent.getFinalSql(this);");
                            code.append("  if (_hpReqId != null) {");
                            code.append("    try {");
                            code.append("       if (com.example.agent.HistoryProvenanceAgent.isWriteOperation(_hpSql)) {");
                            code.append("           java.sql.Connection _hpConn = this.getConnection();");
                            code.append("           if (_hpConn != null) {");
                            code.append("               java.sql.Statement _hpStmt = _hpConn.createStatement();");
                            code.append("               _hpStmt.execute(\"SET request_id = '\" + _hpReqId.replace(\"'\",\"''\") + \"'\");");
                            code.append("               _hpStmt.close();");
                            code.append("           }");
                            code.append("       }");
                            code.append("      com.example.agent.HistoryProvenanceAgent.LOG.println(\"[SQL-EXEC] \" + _hpReqId + \" \" + _hpSql);");
                            code.append("    } catch (Throwable _hpIgnore) {}");
                            code.append("  }");
                            code.append("}");
                            m.insertBefore(code.toString());
                        }
                        
                        // ===== After execution: capture result set; only executeQuery returns ResultSet =====
                        if ("executeQuery".equals(mname) || "getResultSet".equals(mname)) {
                            StringBuilder afterCode = new StringBuilder();
                            afterCode.append("{");
                            //afterCode.append("  com.example.agent.HistoryProvenanceAgent.LOG.println(\"[" + mname + "] captured!\");");
                            afterCode.append("  java.sql.ResultSet _hpRs = $_;\n");
                            afterCode.append("  String _hpReqId = com.example.agent.HistoryProvenanceAgent.getCurrentRequestId();");
                            afterCode.append("  try {");
                            afterCode.append("    if (_hpReqId != null && _hpRs != null) {");
                            // wrapAndRecordRead returns a possibly wrapped ResultSet, which must be assigned back to $_
                            afterCode.append("      $_ = com.example.agent.HistoryProvenanceAgent.wrapAndRecordRead(_hpReqId, _hpRs, this);");
                            afterCode.append("    }");
                            afterCode.append("  } catch (Throwable _hpIgnore) {");
                            afterCode.append("    com.example.agent.HistoryProvenanceAgent.LOG.println(\"[recordRead] Error: \" + _hpIgnore.getMessage());");
                            afterCode.append("  }");
                            afterCode.append("}");
                            m.insertAfter(afterCode.toString(), true);
                        }
                        modified = true;
                    }
                }
            }

            if (modified) {
                byte[] byteCode = ctClass.toBytecode();
                ctClass.detach();
                return byteCode;
            } else {
                ctClass.detach();
                return null;
            }
        }

        // Create ClassPool to handle classloader issues
        private ClassPool createClassPool(ClassLoader loader) {
            ClassPool cp = new ClassPool(true);
            if (loader != null) {
                cp.insertClassPath(new LoaderClassPath(loader));
            }
            cp.appendSystemPath();
            return cp;
        }
    }
}
