-- Outlook.com (Microsoft Graph): per-folder delta/next links and well-known folder ids.
-- {"folders": {"inbox": "<id>", ...}, "sync": {"inbox": {"delta": "<url>"} | {"next": "<url>"}, ...}}
-- Graph links are long opaque URLs, too big for sync_cursor (VARCHAR2(100)) / backfill_token (VARCHAR2(500)).
ALTER TABLE accounts ADD (sync_state JSON)
/
