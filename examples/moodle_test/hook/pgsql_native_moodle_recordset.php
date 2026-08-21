<?php
// This file is part of Moodle - http://moodle.org/
//
// Moodle is free software: you can redistribute it and/or modify
// it under the terms of the GNU General Public License as published by
// the Free Software Foundation, either version 3 of the License, or
// (at your option) any later version.
//
// Moodle is distributed in the hope that it will be useful,
// but WITHOUT ANY WARRANTY; without even the implied warranty of
// MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
// GNU General Public License for more details.
//
// You should have received a copy of the GNU General Public License
// along with Moodle.  If not, see <http://www.gnu.org/licenses/>.

/**
 * Native postgresql recordset.
 *
 * @package    core_dml
 * @copyright  2008 Petr Skoda (http://skodak.org)
 * @license    http://www.gnu.org/copyleft/gpl.html GNU GPL v3 or later
 */

defined('MOODLE_INTERNAL') || die();

require_once(__DIR__.'/moodle_recordset.php');

/**
 * pgsql specific moodle recordset class
 *
 * @package    core_dml
 * @copyright  2008 Petr Skoda (http://skodak.org)
 * @license    http://www.gnu.org/copyleft/gpl.html GNU GPL v3 or later
 */
class pgsql_native_moodle_recordset extends moodle_recordset {

    /** @var PgSql\Result|resource|null */
    protected $result;
    /** @var current row as array.*/
    protected $current;
    protected $blobs = array();

    /** @var string Name of cursor or '' if none */
    protected $cursorname;

    /** @var pgsql_native_moodle_database Postgres database resource */
    protected $db;

    /** @var bool True if there are no more rows to fetch from the cursor */
    protected $lastbatch;

    protected $injection_meta = [];
    protected $read_seq = null;
    protected $snapshot_bounds = null;

    /**
     * Build a new recordset to iterate over.
     *
     * When using cursors, $result will be null initially.
     *
     * @param resource|PgSql\Result|null $result A pg_query() result object to create a recordset from.
     * @param pgsql_native_moodle_database $db Database object (only required when using cursors)
     * @param string $cursorname Name of cursor or '' if none
     * @param array $injection_meta PK injection meta from Rust rewriter
     * @param int|null $read_seq Request-scoped read sequence number
     */
    public function __construct($result, pgsql_native_moodle_database $db = null, $cursorname = '', array $injection_meta = [], $read_seq = null) {
        if ($cursorname && !$db) {
            throw new coding_exception('When specifying a cursor, $db is required');
        }
        $this->result = $result;
        $this->db = $db;
        $this->cursorname = $cursorname;
        $this->injection_meta = $injection_meta;
        $this->read_seq = $read_seq;

        // When there is a cursor, do the initial fetch.
        if ($cursorname) {
            $this->fetch_cursor_block();
        }

        // Find out if there are any blobs.
        $numfields = pg_num_fields($this->result);
        for ($i = 0; $i < $numfields; $i++) {
            $type = $this->db->pg_field_type($this->result, $i);
            if ($type == 'bytea') {
                $this->blobs[] = pg_field_name($this->result, $i);
            }
        }

        $this->current = $this->fetch_next();
    }

    /**
     * Fetches the next block of data when using cursors.
     *
     * @throws coding_exception If you call this when the fetch buffer wasn't freed yet
     */
    protected function fetch_cursor_block() {
        if ($this->result) {
            throw new coding_exception('Unexpected non-empty result when fetching from cursor');
        }
        list($this->result, $this->lastbatch) = $this->db->fetch_from_cursor($this->cursorname);
        if (!$this->result) {
            throw new coding_exception('Unexpected failure when fetching from cursor');
        }
    }

    public function __destruct() {
        $this->close();
    }

    private function fetch_next() {
        if (!$this->result) {
            return false;
        }
        if (!$row = pg_fetch_assoc($this->result)) {
            // There are no more rows in this result.
            pg_free_result($this->result);
            $this->result = null;

            // If using a cursor, can we fetch the next block?
            if ($this->cursorname && !$this->lastbatch) {
                $this->fetch_cursor_block();
                if (!$row = pg_fetch_assoc($this->result)) {
                    pg_free_result($this->result);
                    $this->result = null;
                    return false;
                }
            } else {
                return false;
            }
        }

        if (!empty($this->injection_meta)) {

            $rid = $_SERVER['HTTP_X_REQUEST_ID'] ?? ($GLOBALS['INSTRUMENTATION_RID'] ?? 'SYSTEM');
            $audit_buffer = [];

            // Pop snapshot columns first: xmax then xmin (LIFO tail order)
            $xmax_alias = $this->injection_meta[0]['snapshot_xmax_alias'] ?? INSTRUMENT_SNAPSHOT_XMAX_ALIAS;
            $xmin_alias = $this->injection_meta[0]['snapshot_xmin_alias'] ?? INSTRUMENT_SNAPSHOT_XMIN_ALIAS;

            $xmax_val = null;
            $xmin_val = null;
            if (isset($row[$xmax_alias])) {
                $xmax_val = $row[$xmax_alias];
                unset($row[$xmax_alias]);
            }
            if (isset($row[$xmin_alias])) {
                $xmin_val = $row[$xmin_alias];
                unset($row[$xmin_alias]);
            }

            // Validate snapshot boundary consistency within the same ResultSet
            if ($xmin_val !== null && $xmax_val !== null) {
                if ($this->snapshot_bounds === null) {
                    $this->snapshot_bounds = [(int)$xmin_val, (int)$xmax_val];
                } elseif ($this->snapshot_bounds[0] !== (int)$xmin_val || $this->snapshot_bounds[1] !== (int)$xmax_val) {
                    error_log("[PDO Audit] inconsistent snapshot bounds in recordset: expected " . json_encode($this->snapshot_bounds) . " got [$xmin_val, $xmax_val]");
                }
            }

            // Traverse metadata and strip injected PK columns
            foreach ($this->injection_meta as $meta) {
                $col_name = $meta['alias_used'] ? $meta['alias_used'] : $meta['pk_col'];

                if ($col_name && isset($row[$col_name])) {
                    $val = $row[$col_name];
                    
                    if ($val !== null) {
                        $tbn = $meta['table'] ?? 'unknown';
                        if (!isset($audit_buffer[$tbn])) {
                            $audit_buffer[$tbn] = [];
                        }
                        $audit_buffer[$tbn][$val] = 1;
                    }

                    unset($row[$col_name]); 
                }
            }

            // Write log
            if (!empty($audit_buffer)) {
                $log_str = '';
                $logTime = (new DateTime())->format('Y-m-d\TH:i:s.uP');
                foreach ($audit_buffer as $tbn => $pks) {
                    $entry = [
                        'time' => $logTime,
                        'event' => 'select',
                        'rid'  => $rid,
                        'tbn'  => $tbn,
                        'pks'  => array_keys($pks),
                        'seq'  => $this->read_seq,
                        'xmin' => $this->snapshot_bounds[0] ?? (int)$xmin_val,
                        'xmax' => $this->snapshot_bounds[1] ?? (int)$xmax_val,
                    ];
                    $log_str .= json_encode($entry) . PHP_EOL;
                }
                @file_put_contents('/var/www/html/moodledata/db_access_audit.log', $log_str, FILE_APPEND);
            }
        }

        if ($this->blobs) {
            foreach ($this->blobs as $blob) {
                $row[$blob] = $row[$blob] !== null ? pg_unescape_bytea($row[$blob]) : null;
            }
        }

        return $row;
    }

    public function current(): stdClass {
        return (object)$this->current;
    }

    #[\ReturnTypeWillChange]
    public function key() {
        // return first column value as key
        if (!$this->current) {
            return false;
        }
        $key = reset($this->current);
        return $key;
    }

    public function next(): void {
        $this->current = $this->fetch_next();
    }

    public function valid(): bool {
        return !empty($this->current);
    }

    public function close() {
        if ($this->result) {
            pg_free_result($this->result);
            $this->result  = null;
        }
        $this->current = null;
        $this->blobs   = null;

        // If using cursors, close the cursor.
        if ($this->cursorname) {
            $this->db->close_cursor($this->cursorname);
            $this->cursorname = null;
        }
    }
}
