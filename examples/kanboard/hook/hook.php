<?php

if (defined('INSTRUMENTATION_LOADED')) {
    return;
}
define('INSTRUMENTATION_LOADED', true);

const INSTRUMENT_TABLE_CONFIG = [
    ["table" => "projects", "pk" => "id"],
    ["table" => "users", "pk" => "id"],
    ["table" => "last_logins", "pk" => "id"],
    ["table" => "remember_me", "pk" => "id"],
    ["table" => "project_has_categories", "pk" => "id"],
    ["table" => "columns", "pk" => "id"],
    ["table" => "task_has_files", "pk" => "id"],
    ["table" => "actions", "pk" => "id"],
    ["table" => "comments", "pk" => "id"],
    ["table" => "project_daily_column_stats", "pk" => "id"],
    ["table" => "settings", "pk" => "option"],
    ["table" => "action_has_params", "pk" => "id"],
    ["table" => "subtasks", "pk" => "id"],
    ["table" => "links", "pk" => "id"],
    ["table" => "task_has_links", "pk" => "id"],
    ["table" => "project_daily_stats", "pk" => "id"],
    ["table" => "plugin_schema_versions", "pk" => "plugin"],
    ["table" => "user_has_unread_notifications", "pk" => "id"],
    ["table" => "user_has_notification_types", "pk" => "id"],
    ["table" => "custom_filters", "pk" => "id"],
    ["table" => "swimlanes", "pk" => "id"],
    ["table" => "tasks", "pk" => "id"],
    ["table" => "project_activities", "pk" => "id"],
    ["table" => "transitions", "pk" => "id"],
    ["table" => "subtask_time_tracking", "pk" => "id"],
    ["table" => "project_has_notification_types", "pk" => "id"],
    ["table" => "tags", "pk" => "id"],
    ["table" => "password_reset", "pk" => "token"],
    ["table" => "task_has_external_links", "pk" => "id"],
    ["table" => "project_has_files", "pk" => "id"],
    ["table" => "project_has_roles", "pk" => "role_id"],
    ["table" => "groups", "pk" => "id"],
    ["table" => "project_role_has_restrictions", "pk" => "restriction_id"],
    ["table" => "column_has_restrictions", "pk" => "restriction_id"],
    ["table" => "invites", "pk" => "email"],
    ["table" => "column_has_move_restrictions", "pk" => "restriction_id"],
    ["table" => "predefined_task_descriptions", "pk" => "id"],
    ["table" => "sessions", "pk" => "id"],
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

class RustSqlRewriter {
    private static $ffi = null;
    private static $instance = null;
    private $initialized = false;   // New flag to avoid repeated constructor logic
    
    // Configure your .so path
    const SO_PATH = '/tmp/librust_sql_rewriter.so';

    const TABLE_CONFIG = INSTRUMENT_TABLE_CONFIG;

    private function __construct() {

        if ($this->initialized) return;
        $this->initialized = true;

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

if (!headers_sent()) {
    header("X-Request-ID: {$requestId}");
}

// Ensure the directory exists; otherwise Kanboard may fail due to write permissions
if (!is_dir('/tmp/logs')) {
    @mkdir('/tmp/logs', 0777, true);
}
define('AUDIT_LOG_PATH', '/tmp/logs/db_read.log');
define('SQL_LOG_PATH', '/tmp/logs/sql_exec.log');
define('REQUEST_CONTEXT_LOG_PATH', '/tmp/logs/request_context.log');

// ==========================================
// 2. Priority-v2 request context collection (shutdown callback)
// ==========================================
//
// auto_prepend_file runs before Kanboard bootstrap, so the user object is not initialized yet.
// register_shutdown_function runs after the main script finishes, when $_SESSION is ready.
// Kanboard stores sessions in the DB sessions table (id=session_key, user_id=current user).
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
        $userId = 0;
        $identitySource = 'unknown';

        // 1. Kanboard session, available after PHP session_start
        if (!$userId) {
            $sessionStatus = session_status();
            if ($sessionStatus === PHP_SESSION_NONE) {
                @session_start();
                $sessionStatus = session_status();
            }
            if ($sessionStatus === PHP_SESSION_ACTIVE) {
                $uid = $_SESSION['user_id'] ?? $_SESSION['user']['id'] ?? null;
                if ($uid && (int) $uid > 0) {
                    $userId = (int) $uid;
                    $identitySource = 'php_session';
                }
            }
        }

        // 2. Kanboard DB sessions table; parse session data when the PHP session is unavailable
        if (!$userId) {
            $cookie = $_COOKIE['SESSION'] ?? $_COOKIE[session_name()] ?? null;
            if (!$cookie) {
                foreach ($_COOKIE as $name => $value) {
                    if (preg_match('/SESSION/i', $name) && !empty($value)) {
                        $cookie = $value;
                        break;
                    }
                }
            }
            if ($cookie) {
                try {
                    $dbUrl = getenv('DATABASE_URL') ?: 'postgres://kanboard:kanboard-secret@db/kanboard';
                    if (preg_match('#postgres://([^:]+):([^@]+)@([^:/]+)(?::(\d+))?/(.+)#', $dbUrl, $m)) {
                        $dbh = new PDO(
                            "pgsql:host={$m[3]};port=" . ($m[4] ?? 5432) . ";dbname={$m[5]}",
                            $m[1], $m[2],
                            [PDO::ATTR_ERRMODE => PDO::ERRMODE_SILENT]
                        );
                        $stmt = $dbh->prepare("SELECT data FROM sessions WHERE id = ? LIMIT 1");
                        $stmt->execute([$cookie]);
                        $row = $stmt->fetch(PDO::FETCH_ASSOC);
                        $dbh = null;
                        if ($row && !empty($row['data'])) {
                            // Kanboard session data is PHP serialized format: user|a:N:{s:2:"id";i:X;...}
                            // Extract the user object's id field with a regex
                            if (preg_match('/user\|a:\d+:\{[^}]*s:2:"id";i:(\d+)/', $row['data'], $idMatch)) {
                                $userId = (int) $idMatch[1];
                                $identitySource = 'kanboard_db_session';
                            }
                        }
                    }
                } catch (\Throwable $e) {
                    // ignore
                }
            }
        }

        // Build user_id
        if ($userId > 0) {
            $userIdentity = "user:{$userId}";
        } else {
            // Anonymous user: Session Cookie SHA-256 digest
            $cookie = $_COOKIE['SESSION'] ?? $_COOKIE[session_name()] ?? null;
            if (!$cookie) {
                foreach ($_COOKIE as $name => $value) {
                    if (preg_match('/SESSION/i', $name) && !empty($value)) {
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
                $userIdentity = "request:{$rid}";
                $identitySource = 'request_id';
            }
        }

        // --- Extract api_id ---
        // Kanboard routes through the query string: ?controller=X&action=Y
        // It also supports URL rewrite: /controller/action
        $method = isset($_SERVER['REQUEST_METHOD']) ? strtoupper($_SERVER['REQUEST_METHOD']) : 'GET';
        $controller = $_GET['controller'] ?? '';
        $action = $_GET['action'] ?? '';

        if ($controller && $action) {
            $route = "/{$controller}/{$action}";
        } else {
            // URL rewrite route: extract from PATH_INFO
            $path = $_SERVER['PATH_INFO'] ?? $_SERVER['REQUEST_URI'] ?? '/';
            $path = preg_replace('/\?.*$/', '', $path);
            $path = rtrim($path, '/');
            $route = $path ?: '/';
        }

        $apiId = "{$method} {$route}";

        // --- Write JSONL ---
        $record = [
            'time' => (new \DateTime())->format('Y-m-d\TH:i:s.uP'),
            'rid' => $rid,
            'method' => $method,
            'route' => $route,
            'api_id' => $apiId,
            'user_id' => $userIdentity,
            'identity_source' => $identitySource,
        ];

        $line = json_encode($record, JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE) . PHP_EOL;
        @file_put_contents(REQUEST_CONTEXT_LOG_PATH, $line, FILE_APPEND | LOCK_NB);
    } catch (\Throwable $e) {
        @error_log('[RequestContext] shutdown collect failed: ' . $e->getMessage());
    }
});

// ==========================================
// 3. InstrumentedPDO
// ==========================================

class InstrumentedPDO extends PDO {
    private string $requestId;
    public function __construct($dsn, $username, $passwd, $options = []){

        try {
            $this->requestId = getenv('APP_REQUEST_ID') ?: '';
            // Record attempted connection information
            # file_put_contents('/tmp/logs/sql_exec.log', "[Connect] DSN: $dsn, User: $username\n", FILE_APPEND);
            $options[PDO::ATTR_DEFAULT_FETCH_MODE] = PDO::FETCH_ASSOC;
            // Raise an exception explicitly for debugging
            $options[PDO::ATTR_ERRMODE] = PDO::ERRMODE_EXCEPTION;
            parent::__construct($dsn, $username, $passwd, $options);
            
            $this->setAttribute(PDO::ATTR_STATEMENT_CLASS, [LoggedStatement::class, []]);

        } catch (\PDOException $e) {
            file_put_contents('/tmp/logs/sql_exec.log', "[Connect] FAILED: " . $e->getMessage() . "\n", FILE_APPEND);
            throw $e;
        }

    }

    public function prepare(string $query, array $options = []): PDOStatement|false {
        $this->injectContext($query);
        // FFI rewrite
        $rewriter = RustSqlRewriter::getInstance();
        list($newSql, $metaList) = $rewriter->rewrite($query);
        
        $finalSql = $newSql ?: $query;

        # file_put_contents(SQL_LOG_PATH, "[PREPARE] " . $finalSql . PHP_EOL, FILE_APPEND);

        $stmt = parent::prepare($finalSql, $options);

        // If rewrite occurs, pass metadata for later cleanup
        if ($stmt && !empty($metaList) && $stmt instanceof LoggedStatement) {
            $stmt->setInjectionMeta($metaList, nextInstrumentReadSequence());
        }

        return $stmt;
    }

    public function query(string $query, ?int $fetchMode = null, mixed ...$fetchModeArgs): PDOStatement|false {
        $this->injectContext($query);

        $rewriter = RustSqlRewriter::getInstance();
        list($newSql, $metaList) = $rewriter->rewrite($query);

        $finalSql = $newSql ?: $query;
        # file_put_contents(SQL_LOG_PATH, "[QUERY] " . $finalSql . PHP_EOL, FILE_APPEND);
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
        # file_put_contents(SQL_LOG_PATH, "[EXEC] " . $statement . PHP_EOL, FILE_APPEND);
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
        $logTime =  (new DateTime())->format('Y-m-d\TH:i:s.uP');
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

            // Iterate backward to match array_pop LIFO order
            for ($k = $this->metaCount - 1; $k >= 0; $k--) {
                $val = $this->stripInjectedValue($row, $this->injectionMeta[$k]['alias_used'] ?? null);

                // Record to the buffer
                if ($val !== null) {
                    $tbn = $this->injectionMeta[$k]['table'] ?? 'unknown';
                    if (!isset($auditBuffer[$tbn])) {
                        $auditBuffer[$tbn] = [];
                    }
                    // Use keys as a set for deduplication
                    $auditBuffer[$tbn][$val] = 1;
                }
            }
        }
        unset($row); // Break the reference

        // Write log
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
                    'pks'   => array_keys($pks), // Extract the deduplicated ID list
                    'seq'   => $this->readSequence,
                    'xmin'  => $xmin,
                    'xmax'  => $xmax,
                ];
                $logStr .= json_encode($entry) . PHP_EOL;
            }
            
            // Append with FILE_APPEND
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
