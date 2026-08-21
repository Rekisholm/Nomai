<?php

// 1. Generate a globally unique Request ID (process ID, thread ID, counter, random entropy)
// Note: PHP-FPM usually has no thread ID, so use uniqid instead
if (!defined('INSTRUMENTATION_RID')) {
    $pid = getmypid();
    $rid_str = uniqid("{$pid}-", true);
    define('INSTRUMENTATION_RID', $rid_str);

    // 2. Inject into the PHP global context for later use
    $GLOBALS['INSTRUMENTATION_RID'] = $rid_str;
    $_SERVER['HTTP_X_REQUEST_ID'] = $rid_str;

    // 3. Reset per-request read sequence
    $GLOBALS['INSTRUMENT_READ_SEQUENCE'] = 0;

    // 4. Set a response header for debugging
    header("X-Request-ID: " . $rid_str);
}

// ==========================================
// Priority-v2 request context collection (shutdown callback)
// ==========================================
//
// request_id_hook.php is loaded through require_once in config.php,
// At this point Moodle bootstrap is not complete and the global $USER object is not initialized.
// register_shutdown_function runs after the main script finishes, when $USER is ready.
// Moodle stores the current user in the global $USER object, whose id field is the user primary key.
// Collection failures must fail open and must not affect application responses.

$__moodleContextWritten = false;

register_shutdown_function(function () use (&$__moodleContextWritten) {
    if ($__moodleContextWritten) {
        return;
    }
    $__moodleContextWritten = true;

    try {
        $rid = $GLOBALS['INSTRUMENTATION_RID'] ?? null;
        if (!$rid) {
            return;
        }

        // --- Extract user_id ---
        $userId = 0;
        $identitySource = 'unknown';

        // 1. Moodle global $USER object, initialized in setup.php
        global $USER;
        if (isset($USER) && is_object($USER) && isset($USER->id) && (int) $USER->id > 0) {
            $userId = (int) $USER->id;
            $identitySource = 'moodle_global_user';
        }

        // 2. User id in Moodle $SESSION (fallback)
        if (!$userId) {
            global $SESSION;
            if (isset($SESSION) && is_object($SESSION)) {
                $uid = $SESSION->userid ?? $SESSION->user_id ?? null;
                if ($uid && (int) $uid > 0) {
                    $userId = (int) $uid;
                    $identitySource = 'moodle_session';
                }
            }
        }

        // Build user_id
        if ($userId > 0) {
            $userIdentity = "user:{$userId}";
        } else {
            // Anonymous user: use a SHA-256 digest of the Moodle session cookie
            $cookie = $_COOKIE['MoodleSession'] ?? null;
            if (!$cookie) {
                foreach ($_COOKIE as $name => $value) {
                    if (preg_match('/MoodleSession/i', $name) && !empty($value)) {
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
        // Moodle uses direct PHP file routing, so SCRIPT_NAME is the stable logical endpoint.
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
        @file_put_contents('/var/www/html/moodledata/request_context.log', $line, FILE_APPEND | LOCK_NB);
    } catch (\Throwable $e) {
        @error_log('[RequestContext] shutdown collect failed: ' . $e->getMessage());
    }
});
