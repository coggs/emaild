-- F5 Trackers: status boards configured in plain language (orders, service status, ticket on-sales, custom).
-- A tracker's `compiled` reuses the rule `match` (rules.py) plus its kind, notify overrides and timings.
CREATE TABLE trackers (
  id             NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id      NUMBER DEFAULT ON NULL TO_NUMBER(SYS_CONTEXT('EMAILD_CTX','TENANT_ID')) NOT NULL,
  user_id        NUMBER DEFAULT ON NULL TO_NUMBER(SYS_CONTEXT('EMAILD_CTX','USER_ID')) NOT NULL,
  name           VARCHAR2(200 CHAR) NOT NULL,
  original_text  VARCHAR2(2000 CHAR) NOT NULL,       -- the user's own words
  compiled       JSON NOT NULL,                      -- {"kind","match","notify_on","silent","states","close_after_days","cadence_days"}
  readback       VARCHAR2(2000 CHAR),                -- plain English generated from `compiled`
  kind           VARCHAR2(10) NOT NULL CHECK (kind IN ('orders','service','onsale','custom')),
  status         VARCHAR2(10) DEFAULT 'pending' NOT NULL CHECK (status IN ('pending','active','paused','deleted')),
  version        NUMBER DEFAULT 1 NOT NULL,
  history        JSON,                               -- earlier wordings: [{"version","original_text","at"}]
  created_at     TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
  updated_at     TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
  last_event_at  TIMESTAMP WITH TIME ZONE
)
/
CREATE INDEX trackers_status_ix ON trackers (user_id, status)
/
-- One row per tracked thing: an order, a service component, an event on sale.
CREATE TABLE tracker_items (
  id               NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id        NUMBER DEFAULT ON NULL TO_NUMBER(SYS_CONTEXT('EMAILD_CTX','TENANT_ID')) NOT NULL,
  user_id          NUMBER DEFAULT ON NULL TO_NUMBER(SYS_CONTEXT('EMAILD_CTX','USER_ID')) NOT NULL,
  tracker_id       NUMBER NOT NULL REFERENCES trackers(id) ON DELETE CASCADE,
  item_key         VARCHAR2(400 CHAR) NOT NULL,      -- normalised order number / service+component / event name
  title            VARCHAR2(400 CHAR),
  fields           JSON,                             -- per-kind fields (validated in Python; URLs https only)
  state            VARCHAR2(40) NOT NULL,
  state_rank       NUMBER DEFAULT 0 NOT NULL,        -- rank of the last main-progression state (forward only)
  first_seen_at    TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
  last_changed_at  TIMESTAMP WITH TIME ZONE,         -- when the current state happened (email time)
  last_heard_at    TIMESTAMP WITH TIME ZONE,         -- newest email about it, change or not
  closed_at        TIMESTAMP WITH TIME ZONE,         -- null while on the board
  last_email_id    NUMBER REFERENCES items(id) ON DELETE SET NULL,
  CONSTRAINT tracker_items_uk UNIQUE (tracker_id, item_key)
)
/
CREATE INDEX tracker_items_open_ix ON tracker_items (user_id, tracker_id, closed_at)
/
-- Every email a tracker consumed (change / repeat / stale / irrelevant), plus reminders and silence warnings.
-- (tracker_id, email_item_id) is how the pipeline knows an email was already read for that tracker.
CREATE TABLE tracker_events (
  id               NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id        NUMBER DEFAULT ON NULL TO_NUMBER(SYS_CONTEXT('EMAILD_CTX','TENANT_ID')) NOT NULL,
  user_id          NUMBER DEFAULT ON NULL TO_NUMBER(SYS_CONTEXT('EMAILD_CTX','USER_ID')) NOT NULL,
  tracker_id       NUMBER NOT NULL REFERENCES trackers(id) ON DELETE CASCADE,
  tracker_item_id  NUMBER REFERENCES tracker_items(id) ON DELETE CASCADE,
  email_item_id    NUMBER REFERENCES items(id) ON DELETE SET NULL,
  outcome          VARCHAR2(12) NOT NULL
                   CHECK (outcome IN ('change','repeat','stale','irrelevant','reminder','silence')),
  old_state        VARCHAR2(40),
  new_state        VARCHAR2(40),
  fields           JSON,
  notify           BOOLEAN DEFAULT FALSE NOT NULL,   -- this event goes to Telegram (policy + fresh enough)
  occurred_at      TIMESTAMP WITH TIME ZONE,
  created_at       TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
  notified_at      TIMESTAMP WITH TIME ZONE
)
/
CREATE INDEX tracker_events_email_ix ON tracker_events (tracker_id, email_item_id)
/
CREATE INDEX tracker_events_notify_ix ON tracker_events (user_id, notify, notified_at)
/
-- Suggested trackers ("You get order emails from Acme Shop; track them?"), and what the user did with them.
-- skey is "<kind>:<domain>"; a dismissed key is never suggested again.
CREATE TABLE tracker_suggestions (
  id          NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id   NUMBER DEFAULT ON NULL TO_NUMBER(SYS_CONTEXT('EMAILD_CTX','TENANT_ID')) NOT NULL,
  user_id     NUMBER DEFAULT ON NULL TO_NUMBER(SYS_CONTEXT('EMAILD_CTX','USER_ID')) NOT NULL,
  skey        VARCHAR2(400) NOT NULL,
  kind        VARCHAR2(10) NOT NULL CHECK (kind IN ('orders','service')),
  label       VARCHAR2(200 CHAR),
  text        VARCHAR2(2000 CHAR) NOT NULL,          -- ready-made tracker text ("Track my orders from ...")
  evidence    VARCHAR2(1000 CHAR),
  status      VARCHAR2(10) DEFAULT 'open' NOT NULL CHECK (status IN ('open','accepted','dismissed')),
  tracker_id  NUMBER REFERENCES trackers(id) ON DELETE SET NULL,
  created_at  TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
  acted_at    TIMESTAMP WITH TIME ZONE,
  CONSTRAINT tracker_suggestions_uk UNIQUE (user_id, skey)
)
/
BEGIN
  FOR t IN (SELECT column_value AS tname FROM TABLE(sys.odcivarchar2list('TRACKERS','TRACKER_ITEMS',
                                                                         'TRACKER_EVENTS','TRACKER_SUGGESTIONS'))) LOOP
    DBMS_RLS.ADD_POLICY(object_schema => USER, object_name => t.tname, policy_name => 'USER_SCOPE',
                        function_schema => USER, policy_function => 'VPD_USER_SCOPE',
                        statement_types => 'SELECT,INSERT,UPDATE,DELETE', update_check => TRUE,
                        policy_type => DBMS_RLS.DYNAMIC);
    EXECUTE IMMEDIATE 'GRANT SELECT, INSERT, UPDATE, DELETE ON ' || t.tname || ' TO email_app';
  END LOOP;
END;
/
