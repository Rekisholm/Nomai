package com.example.agent;

/**
 * SqlRewriteJni: JNI wrapper for interacting with the Rust layer (independent top-level class)
 */
public class SqlRewriteJni {

    // 1. Define return structure (static inner class)
    public static class RewriteResult {
        public String newSql;
        public String metaJson;
        public String errorMsg;

        // This constructor is called by Rust
        public RewriteResult(String newSql, String metaJson, String errorMsg) {
            this.newSql = newSql;
            this.metaJson = metaJson;
            this.errorMsg = errorMsg;
        }

        public boolean isSuccess() {
            return errorMsg == null || errorMsg.isEmpty();
        }
    }

    // 2. Static block: load dynamic library
    static {
        loadLibrary();
    }

    private static void loadLibrary() {
        try {
            System.load("/libjni_sql_rewriter.so");
        } catch (UnsatisfiedLinkError e) {
            System.err.println("[SqlRewriteAgent] Failed to link native library.");
            e.printStackTrace();
            throw new RuntimeException(e);
        }
    }

    // 3. Native method declaration, restoring the original JNI signature
    private static native void initConfig(String configJson);
    private static native RewriteResult rewriteSql(String sql);

    // 4. Public wrapper method with safety checks
    public static void initTableConfig(String configJson) {
        if (configJson == null || configJson.isEmpty()) {
            throw new IllegalArgumentException("Config JSON cannot be empty");
        }
        try {
            initConfig(configJson);
        } catch (Exception e) {
            throw new RuntimeException("Failed to init table config: " + e.getMessage());
        }
    }

    public static RewriteResult safeRewriteSql(String sql) {
        if (sql == null) return new RewriteResult("", "", "SQL is null");
        try {
            return rewriteSql(sql);
        } catch (Throwable t) {
            t.printStackTrace();
            return new RewriteResult(sql, "[]", "Native Error: " + t.getMessage());
        }
    }
}