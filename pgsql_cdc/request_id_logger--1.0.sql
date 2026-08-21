CREATE FUNCTION nomai_snapshot_bounds_xmin()
RETURNS bigint
AS 'MODULE_PATHNAME', 'nomai_snapshot_bounds_xmin'
LANGUAGE C VOLATILE PARALLEL RESTRICTED;

CREATE FUNCTION nomai_snapshot_bounds_xmax()
RETURNS bigint
AS 'MODULE_PATHNAME', 'nomai_snapshot_bounds_xmax'
LANGUAGE C VOLATILE PARALLEL RESTRICTED;

COMMENT ON FUNCTION nomai_snapshot_bounds_xmin() IS
    'xmin of the snapshot active in the current statement executor';
COMMENT ON FUNCTION nomai_snapshot_bounds_xmax() IS
    'xmax of the snapshot active in the current statement executor';
