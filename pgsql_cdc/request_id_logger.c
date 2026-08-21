/*
 * build for postgres:13.12
 */

#include "postgres.h"
#include "fmgr.h"
#include "access/xact.h"
#include "utils/guc.h"
#include "executor/spi.h"
#include "utils/builtins.h"
#include "utils/snapmgr.h"

PG_MODULE_MAGIC;

PG_FUNCTION_INFO_V1(nomai_snapshot_bounds_xmin);
PG_FUNCTION_INFO_V1(nomai_snapshot_bounds_xmax);

static char *request_id = NULL;

static void request_id_xact_callback(XactEvent event, void *arg);

/*
 * Return the bounds of the snapshot that is already active for the current
 * executor invocation.  Do not replace this with GetTransactionSnapshot():
 * that API is allowed to acquire a snapshot at the time of the function call,
 * while provenance needs the exact snapshot used by the running statement.
 */
static Snapshot
nomai_get_active_snapshot(void)
{
    Snapshot snapshot = GetActiveSnapshot();

    if (snapshot == NULL)
        ereport(ERROR,
                (errcode(ERRCODE_OBJECT_NOT_IN_PREREQUISITE_STATE),
                 errmsg("nomai snapshot bounds require an active statement snapshot")));

    return snapshot;
}

Datum
nomai_snapshot_bounds_xmin(PG_FUNCTION_ARGS)
{
    Snapshot snapshot = nomai_get_active_snapshot();

    PG_RETURN_INT64((int64) snapshot->xmin);
}

Datum
nomai_snapshot_bounds_xmax(PG_FUNCTION_ARGS)
{
    Snapshot snapshot = nomai_get_active_snapshot();

    PG_RETURN_INT64((int64) snapshot->xmax);
}

void
_PG_init(void)
{
    DefineCustomStringVariable(
        "request_id",
        "Optional request identifier used for CDC tracing.",
        NULL,
        &request_id,
        "",
        PGC_USERSET,
        0,
        NULL, NULL, NULL);

    RegisterXactCallback(request_id_xact_callback, NULL);
}

static void
request_id_xact_callback(XactEvent event, void *arg)
{
    /* Run during PRE_COMMIT. */
    if (event == XACT_EVENT_PRE_COMMIT)
    {
        if (request_id && *request_id)
        {
            char sql[512];
            snprintf(sql, sizeof(sql),
                     "SELECT pg_logical_emit_message(true,'request','{\"request_id\":\"%s\"}');",
                     request_id);

            if (SPI_connect() == SPI_OK_CONNECT)
            {
                /* Snapshot only for the PRE_COMMIT SPI message, not provenance. */
                PushActiveSnapshot(GetTransactionSnapshot());
                (void) SPI_execute(sql, false, 0);
                PopActiveSnapshot();
                SPI_finish();
            }
        }
    }
}
