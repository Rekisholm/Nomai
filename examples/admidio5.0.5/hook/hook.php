<?php

if (defined('INSTRUMENTATION_LOADED')) {
    return;
}
define('INSTRUMENTATION_LOADED', true);

const INSTRUMENT_TABLE_CONFIG = [
    ["table" => "adm_auto_login", "pk" => "atl_id"],
    ["table" => "adm_category_report", "pk" => "crt_id"],
    ["table" => "adm_events", "pk" => "dat_id"],
    ["table" => "adm_folders", "pk" => "fol_id"],
    ["table" => "adm_components", "pk" => "com_id"],
    ["table" => "adm_categories", "pk" => "cat_id"],
    ["table" => "adm_files", "pk" => "fil_id"],
    ["table" => "adm_guestbook_comments", "pk" => "gbc_id"],
    ["table" => "adm_lists", "pk" => "lst_id"],
    ["table" => "adm_members", "pk" => "mem_id"],
    ["table" => "adm_links", "pk" => "lnk_id"],
    ["table" => "adm_list_columns", "pk" => "lsc_id"],
    ["table" => "adm_messages", "pk" => "msg_id"],
    ["table" => "adm_messages_content", "pk" => "msc_id"],
    ["table" => "adm_messages_recipients", "pk" => "msr_id"],
    ["table" => "adm_messages_attachments", "pk" => "msa_id"],
    ["table" => "adm_menu", "pk" => "men_id"],
    ["table" => "adm_photos", "pk" => "pho_id"],
    ["table" => "adm_roles_rights", "pk" => "ror_id"],
    ["table" => "adm_role_dependencies", "pk" => "rld_rol_id_parent"],
    ["table" => "adm_roles_rights_data", "pk" => "rrd_id"],
    ["table" => "adm_roles", "pk" => "rol_id"],
    ["table" => "adm_texts", "pk" => "txt_id"],
    ["table" => "adm_user_data", "pk" => "usd_id"],
    ["table" => "adm_sessions", "pk" => "ses_id"],
    ["table" => "adm_user_relation_types", "pk" => "urt_id"],
    ["table" => "adm_user_fields", "pk" => "usf_id"],
    ["table" => "adm_user_relations", "pk" => "ure_id"],
    ["table" => "adm_announcements", "pk" => "ann_id"],
    ["table" => "adm_rooms", "pk" => "room_id"],
    ["table" => "adm_guestbook", "pk" => "gbo_id"],
    ["table" => "adm_preferences", "pk" => "prf_id"],
    ["table" => "adm_registrations", "pk" => "reg_id"],
    ["table" => "adm_organizations", "pk" => "org_id"],
    ["table" => "adm_log_changes", "pk" => "log_id"],
    ["table" => "adm_forum_topics", "pk" => "fot_id"],
    ["table" => "adm_forum_posts", "pk" => "fop_id"],
    ["table" => "adm_users", "pk" => "usr_id"],
    ["table" => "adm_saml_clients", "pk" => "smc_id"],
    ["table" => "adm_sso_keys", "pk" => "key_id"],
    ["table" => "adm_oidc_clients", "pk" => "ocl_id"],
    ["table" => "adm_oidc_access_tokens", "pk" => "oat_id"],
    ["table" => "adm_oidc_refresh_tokens", "pk" => "ort_id"],
    ["table" => "adm_oidc_auth_codes", "pk" => "oac_id"],
    ["table" => "adm_user_field_select_options", "pk" => "ufo_id"],
    ["table" => "adm_inventory_item_data", "pk" => "ind_id"],
    ["table" => "adm_inventory_items", "pk" => "ini_id"],
    ["table" => "adm_inventory_item_borrow_data", "pk" => "inb_id"],
    ["table" => "adm_inventory_field_select_options", "pk" => "ifo_id"],
    ["table" => "adm_inventory_fields", "pk" => "inf_id"],
];

const INSTRUMENT_SNAPSHOT_COLUMN_COUNT = 2;
const INSTRUMENT_SNAPSHOT_XMIN_ALIAS = '__nomai_xmin';
const INSTRUMENT_SNAPSHOT_XMAX_ALIAS = '__nomai_xmax';

$GLOBALS['INSTRUMENT_READ_SEQUENCE'] = 0;

function nextInstrumentReadSequence(): int {
    $seq = $GLOBALS['INSTRUMENT_READ_SEQUENCE'] ?? 0;
    $GLOBALS['INSTRUMENT_READ_SEQUENCE'] = $seq + 1;
    return $seq;
}

if (!is_dir('/tmp/logs')) {
    @mkdir('/tmp/logs', 0777, true);
}

class RustSqlRewriter {
    private static $ffi = null;
    private static $instance = null;
    
    // Configure your .so path
    const SO_PATH = '/tmp/librust_sql_rewriter.so';

    const TABLE_CONFIG = INSTRUMENT_TABLE_CONFIG;

    private function __construct() {
        if (!file_exists(self::SO_PATH)) {
            error_log("[RustRewriter] .so file not found: " . self::SO_PATH);
            return;
        }

        try {
            // Define the C interface signature
            $cdef = "
                typedef struct {
                    char* new_sql;
                    char* meta_json;
                    char* error_msg;
                } RewriteResult;

                int init_config(const char* json);
                RewriteResult rewrite_sql(const char* sql);
                void free_rewrite_result(RewriteResult res);
            ";

            self::$ffi = FFI::cdef($cdef, self::SO_PATH);

            // Initialize configuration
            $json_config = json_encode(self::TABLE_CONFIG);
            if (self::$ffi->init_config($json_config) != 0) {
                error_log("[RustRewriter] Failed to init config");
            } else {
                error_log("[RustRewriter] Initialized successfully");
            }
        } catch (Exception $e) {
            error_log("[RustRewriter] FFI Load Error: " . $e->getMessage());
            self::$ffi = null;
        }
    }

    public static function getInstance() {
        if (self::$instance === null) {
            self::$instance = new self();
        }
        return self::$instance;
    }

    /**
     * Call Rust to rewrite SQL
     * @return array [string|null $new_sql, array $meta_list]
     */
    public function rewrite($sql) {
        if (!self::$ffi) {
            return [null, []];
        }
        if (stripos(trim($sql), 'SELECT') !== 0) {
            return [null, []];
        }

        try {
            $res = self::$ffi->rewrite_sql($sql);
            
            $new_sql = null;
            $meta = [];

            if ($res->error_msg !== null) {
                // $err = FFI::string($res->error_msg);
                // error_log("[RustRewriter] Rewrite error: $err");
            } elseif ($res->new_sql !== null) {
                $new_sql = FFI::string($res->new_sql);
                if ($res->meta_json !== null) {
                    $json_str = FFI::string($res->meta_json);
                    $meta = json_decode($json_str, true) ?? [];
                }
            }

            // Release memory allocated on the Rust side
            self::$ffi->free_rewrite_result($res);

            return [$new_sql, $meta];
        } catch (Exception $e) {
            error_log("[RustRewriter] Call Error: " . $e->getMessage());
            return [null, []];
        }
    }
}

// 1. Generate Request ID
$requestId = sprintf('%s-%s-%s', getmypid(), time(), bin2hex(random_bytes(4)));
putenv("APP_REQUEST_ID={$requestId}");
$_SERVER['HTTP_X_REQUEST_ID'] = $requestId;

// Reset per-request read sequence
$GLOBALS['INSTRUMENT_READ_SEQUENCE'] = 0;

if (!headers_sent()) {
    header("X-Request-ID: {$requestId}");
}

define('AUDIT_LOG_PATH', '/tmp/logs/db_read.log');

define('SQL_LOG_PATH', '/tmp/logs/sql_exec.log');

define('REQUEST_CONTEXT_LOG_PATH', '/tmp/logs/request_context.log');

// ==========================================
// 2. Priority-v2 request context collection (shutdown callback)
// ==========================================
//
// auto_prepend_file runs before Admidio bootstrap (common.php),
// so $gCurrentUser / $gCurrentSession are not initialized yet.
// register_shutdown_function runs after the main script, including common.php, finishes,
// when globals are ready; this is the right time to collect user_id.
// Collection failures must fail open and must not affect application responses.

$__contextWritten = false;

register_shutdown_function(function () use (&$__contextWritten) {
    if ($__contextWritten) {
        return;
    }
    $__contextWritten = true;

    try {
        $rid = getenv('APP_REQUEST_ID');
        if (!$rid) {
            return;
        }

        // --- Extract user_id ---
        // Prefer globals precomputed in common.php
        $userId = 0;
        $identitySource = 'unknown';

        if (isset($GLOBALS['gCurrentUserId']) && (int) $GLOBALS['gCurrentUserId'] > 0) {
            $userId = (int) $GLOBALS['gCurrentUserId'];
            $identitySource = 'admidio_global';
        }

        // Fallback: read ses_usr_id from the Session object
        if (!$userId && isset($GLOBALS['gCurrentSession'])) {
            try {
                $sesUsrId = $GLOBALS['gCurrentSession']->getValue('ses_usr_id');
                if ($sesUsrId && (int) $sesUsrId > 0) {
                    $userId = (int) $sesUsrId;
                    $identitySource = 'admidio_session';
                }
            } catch (\Throwable $e) {
                // Ignore
            }
        }

        // Fallback: read usr_id from the User object
        if (!$userId && isset($GLOBALS['gCurrentUser'])) {
            try {
                $usrId = $GLOBALS['gCurrentUser']->getValue('usr_id');
                if ($usrId && (int) $usrId > 0) {
                    $userId = (int) $usrId;
                    $identitySource = 'admidio_user';
                }
            } catch (\Throwable $e) {
                // Ignore
            }
        }

        // Authenticated user
        if ($userId > 0) {
            $userIdentity = "user:{$userId}";
        } else {
            // Anonymous user: use a SHA-256 digest of the Session Cookie
            $sessionName = function_exists('session_name') ? @session_name() : '';
            $cookie = null;
            if ($sessionName && !empty($_COOKIE[$sessionName])) {
                $cookie = $_COOKIE[$sessionName];
            }
            // Fallback: find a cookie ending with _SESSION_ID
            if (!$cookie) {
                foreach ($_COOKIE as $name => $value) {
                    if (str_ends_with($name, '_SESSION_ID') && !empty($value)) {
                        $cookie = $value;
                        break;
                    }
                }
            }

            if ($cookie) {
                $digest = hash('sha256', $cookie);
                $userIdentity = 'session:' . substr($digest, 0, 24);
                $identitySource = 'session_cookie_sha256';
            } else {
                // If neither user nor cookie exists, fall back to request_id (degrades to v1)
                $userIdentity = "request:{$rid}";
                $identitySource = 'request_id';
            }
        }

        // --- Extract api_id ---
        // Admidio uses direct PHP file routing, so SCRIPT_NAME is the stable logical endpoint.
        // It contains no query string or resource ID, so no extra normalization is needed.
        $method = isset($_SERVER['REQUEST_METHOD']) ? strtoupper($_SERVER['REQUEST_METHOD']) : 'GET';
        $script = $_SERVER['SCRIPT_NAME'] ?? ($_SERVER['PHP_SELF'] ?? '/');
        $apiId = "{$method} {$script}";

        // --- Write JSONL ---
        $record = [
            'time' => (new \DateTime())->format('Y-m-d\TH:i:s.uP'),
            'rid' => $rid,
            'method' => $method,
            'route' => $script,
            'api_id' => $apiId,
            'user_id' => $userIdentity,
            'identity_source' => $identitySource,
        ];

        $line = json_encode($record, JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE) . PHP_EOL;
        @file_put_contents(REQUEST_CONTEXT_LOG_PATH, $line, FILE_APPEND | LOCK_NB);
    } catch (\Throwable $e) {
        // Fail open: only record diagnostics and do not affect application behavior
        @error_log('[RequestContext] shutdown collect failed: ' . $e->getMessage());
    }
});

// ==========================================
// 3. InstrumentedPDO
// ==========================================

class InstrumentedPDO extends PDO {
    
    public function __construct($dsn, $username, $passwd, $options = []) {
        parent::__construct($dsn, $username, $passwd, $options);
    }

    public function prepare(string $query, array $options = []): PDOStatement|false {
        
        file_put_contents(SQL_LOG_PATH, $query . PHP_EOL, FILE_APPEND);
        // FFI rewrite
        $rewriter = RustSqlRewriter::getInstance();
        list($newSql, $metaList) = $rewriter->rewrite($query);

        $finalSql = $newSql ?: $query;

        $this->injectContext($finalSql);

        $stmt = parent::prepare($finalSql, $options);

        // If rewrite occurs, pass metadata for later cleanup
        if ($stmt && !empty($metaList) && $stmt instanceof LoggedStatement) {
            $stmt->setInjectionMeta($metaList, nextInstrumentReadSequence());
        }

        return $stmt;
    }

    public function query(string $query, ?int $fetchMode = null, mixed ...$fetchModeArgs): PDOStatement|false {

        file_put_contents(SQL_LOG_PATH, $query . PHP_EOL, FILE_APPEND);
        $rewriter = RustSqlRewriter::getInstance();
        list($newSql, $metaList) = $rewriter->rewrite($query);

        $finalSql = $newSql ?: $query;

        $this->injectContext($finalSql);

        if ($fetchMode !== null) {
            $stmt = parent::query($finalSql, $fetchMode, ...$fetchModeArgs);
        } else {
            $stmt = parent::query($finalSql);
        }

        if ($stmt && !empty($metaList) && $stmt instanceof LoggedStatement) {
            $stmt->setInjectionMeta($metaList, nextInstrumentReadSequence());
        }

        return $stmt;
    }

    public function exec(string $statement): int|false {
        
        if (stripos(trim($statement), 'SET') === 0) {
            return parent::exec($statement);
        }
        $this->injectContext($statement);
        return parent::exec($statement);
    }

    private function injectContext(string $query) {
        $rid = getenv('APP_REQUEST_ID');
        if ($rid) {
            if(stripos(trim($query), 'UPDATE') === 0 || stripos(trim($query), 'INSERT') === 0 || stripos(trim($query), 'DELETE') === 0) {
                try {
                    $safeRid = $this->quote($rid);
                    parent::exec("SET request_id = $safeRid");
                } catch (Exception $e) {}
            }
        }
    }
}

// ==========================================
// 4. LoggedStatement (implements LIFO cleanup logic)
// ==========================================

class LoggedStatement extends PDOStatement {

    private $injectionMeta = [];
    private $metaCount = 0;
    private $readSequence = null;
    private $snapshotBounds = null;

    protected function __construct() {}

    public function setInjectionMeta(array $metaList, ?int $readSequence = null) {
        $this->injectionMeta = $metaList;
        $this->metaCount = count($metaList);
        $this->readSequence = $readSequence ?? 0;
        $this->snapshotBounds = null;
    }

    public function columnCount(): int {
        $count = parent::columnCount();
        if ($this->metaCount > 0) {
            $hiddenCount = $this->metaCount + INSTRUMENT_SNAPSHOT_COLUMN_COUNT;
            if ($count >= $hiddenCount) {
                return $count - $hiddenCount;
            }
        }
        return $count;
    }

    public function fetchAll(int $mode = PDO::FETCH_DEFAULT, ...$args): array {
        $rows = parent::fetchAll($mode, ...$args);

        if ($this->metaCount > 0 && !empty($rows)) {
            $this->processRowsAndLog($rows);
        }

        return $rows;
    }

    public function fetch(int $mode = PDO::FETCH_DEFAULT, int $cursorOrientation = PDO::FETCH_ORI_NEXT, int $cursorOffset = 0): mixed {
        $row = parent::fetch($mode, $cursorOrientation, $cursorOffset);

        if ($this->metaCount > 0 && $row) {
            $wrapper = [$row];
            $this->processRowsAndLog($wrapper);
            $row = $wrapper[0];
        }
        return $row;
    }

    public function fetchObject(?string $class = "stdClass", array $constructorArgs = []): object|false {
        $row = parent::fetchObject($class, $constructorArgs);

        if ($this->metaCount > 0 && $row) {
            $wrapper = [$row];
            $this->processRowsAndLog($wrapper);
            $row = $wrapper[0];
        }
        return $row;
    }

    private function processRowsAndLog(array &$rows) {
        $rid = getenv('APP_REQUEST_ID');
        $logTime = (new DateTime())->format('Y-m-d\TH:i:s.uP');
        $auditBuffer = [];
        $batchSnapshotBounds = [];
        $snapshotError = false;

        foreach ($rows as &$row) {
            $bounds = $this->stripSnapshotBounds($row);
            if ($bounds === null) {
                $snapshotError = true;
            } else {
                [$xmin, $xmax] = $bounds;
                $batchSnapshotBounds[$xmin . ':' . $xmax] = [$xmin, $xmax];
                if (!$this->registerSnapshotBounds($xmin, $xmax)) {
                    $snapshotError = true;
                }
            }

            for ($k = $this->metaCount - 1; $k >= 0; $k--) {
                $targetColName = $this->injectionMeta[$k]['alias_used'] ?? null;
                $val = $this->stripInjectedValue($row, $targetColName);

                if ($val !== null) {
                    $tbn = $this->injectionMeta[$k]['table'] ?? 'unknown';
                    if (!isset($auditBuffer[$tbn])) {
                        $auditBuffer[$tbn] = [];
                    }
                    $auditBuffer[$tbn][$val] = 1;
                }
            }
        }
        unset($row);

        if (!empty($auditBuffer)) {
            if ($snapshotError || count($batchSnapshotBounds) !== 1) {
                error_log("[PDO Audit] inconsistent snapshot bounds: " . json_encode(array_values($batchSnapshotBounds)));
                return;
            }
            [$xmin, $xmax] = reset($batchSnapshotBounds);

            $logStr = '';
            foreach ($auditBuffer as $tbn => $pks) {
                $entry = [
                    'time'  => $logTime,
                    'event' => 'select',
                    'rid'   => $rid,
                    'tbn'   => $tbn,
                    'pks'   => array_keys($pks),
                    'seq'   => $this->readSequence,
                    'xmin'  => $xmin,
                    'xmax'  => $xmax,
                ];
                $logStr .= json_encode($entry) . PHP_EOL;
            }
            file_put_contents(AUDIT_LOG_PATH, $logStr, FILE_APPEND);
        }
    }

    private function stripSnapshotBounds(&$row): ?array {
        $xminAlias = $this->snapshotAlias('snapshot_xmin_alias', INSTRUMENT_SNAPSHOT_XMIN_ALIAS);
        $xmaxAlias = $this->snapshotAlias('snapshot_xmax_alias', INSTRUMENT_SNAPSHOT_XMAX_ALIAS);

        $xmax = $this->stripInjectedValue($row, $xmaxAlias);
        $xmin = $this->stripInjectedValue($row, $xminAlias);

        if ($xmin === null || $xmax === null || $xmin === '' || $xmax === '') {
            return null;
        }

        return [(int) $xmin, (int) $xmax];
    }

    private function snapshotAlias(string $metaKey, string $fallback): string {
        if (!empty($this->injectionMeta) && !empty($this->injectionMeta[0][$metaKey])) {
            return $this->injectionMeta[0][$metaKey];
        }
        return $fallback;
    }

    private function registerSnapshotBounds(int $xmin, int $xmax): bool {
        if ($this->snapshotBounds === null) {
            $this->snapshotBounds = [$xmin, $xmax];
            return true;
        }
        return $this->snapshotBounds[0] === $xmin && $this->snapshotBounds[1] === $xmax;
    }

    private function stripInjectedValue(&$row, ?string $alias) {
        if (is_array($row)) {
            return $this->stripInjectedValueFromArray($row, $alias);
        }
        if (is_object($row)) {
            return $this->stripInjectedValueFromObject($row, $alias);
        }
        return null;
    }

    private function stripInjectedValueFromArray(array &$row, ?string $alias) {
        $value = null;
        $found = false;
        $foundKey = $this->findArrayKeyByAlias($row, $alias);

        if ($foundKey !== null) {
            $value = $row[$foundKey];
            unset($row[$foundKey]);
            $found = true;
        }

        $numericValue = $this->popLastNumericValue($row);
        if (!$found && $numericValue !== null) {
            $value = $numericValue;
            $found = true;
        }

        return $found ? $value : null;
    }

    private function stripInjectedValueFromObject(object $row, ?string $alias) {
        $foundProp = $this->findObjectPropertyByAlias($row, $alias);
        if ($foundProp === null) {
            return null;
        }

        $value = $row->$foundProp;
        unset($row->$foundProp);
        return $value;
    }

    private function findArrayKeyByAlias(array $row, ?string $alias) {
        if (!$alias) {
            return null;
        }
        if (array_key_exists($alias, $row)) {
            return $alias;
        }

        $rowKeys = array_keys($row);
        for ($i = count($rowKeys) - 1; $i >= 0; $i--) {
            $currentKey = $rowKeys[$i];
            if (is_string($currentKey) && strpos($currentKey, $alias) === 0) {
                return $currentKey;
            }
        }

        return null;
    }

    private function findObjectPropertyByAlias(object $row, ?string $alias): ?string {
        if (!$alias) {
            return null;
        }
        if (property_exists($row, $alias)) {
            return $alias;
        }

        $props = array_keys(get_object_vars($row));
        for ($i = count($props) - 1; $i >= 0; $i--) {
            if (strpos($props[$i], $alias) === 0) {
                return $props[$i];
            }
        }

        return null;
    }

    private function popLastNumericValue(array &$row) {
        $lastKey = null;
        foreach (array_keys($row) as $key) {
            if (is_int($key) && ($lastKey === null || $key > $lastKey)) {
                $lastKey = $key;
            }
        }

        if ($lastKey === null || !array_key_exists($lastKey, $row)) {
            return null;
        }

        $value = $row[$lastKey];
        unset($row[$lastKey]);
        return $value;
    }
}