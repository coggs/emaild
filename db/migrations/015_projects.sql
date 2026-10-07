-- Phase 3 slice 1: projects (umbrellas and sub-projects), the emails filed into them, the facts pulled out of those
-- emails, a per-project timeline, and suggested sub-projects. `compiled.match` reuses the rule match (rules.py).
-- Every table is user-scoped by VPD like the rest of the schema.
CREATE TABLE projects (
  id                NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id         NUMBER DEFAULT ON NULL TO_NUMBER(SYS_CONTEXT('EMAILD_CTX','TENANT_ID')) NOT NULL,
  user_id           NUMBER DEFAULT ON NULL TO_NUMBER(SYS_CONTEXT('EMAILD_CTX','USER_ID')) NOT NULL,
  parent_id         NUMBER REFERENCES projects(id) ON DELETE SET NULL,
  name              VARCHAR2(200 CHAR) NOT NULL,
  aliases           JSON,                               -- ["the committee", "NSFC"]
  description       VARCHAR2(1000 CHAR),                -- also the topic Gemma uses to sort mail into sub-projects
  kind              VARCHAR2(10) DEFAULT 'umbrella' NOT NULL CHECK (kind IN ('umbrella','project')),
  status            VARCHAR2(10) DEFAULT 'pending' NOT NULL
                    CHECK (status IN ('pending','active','done','archived','deleted')),
  compiled          JSON,                               -- {"match": <rule match>|null, "topic", "seed_items": [...]}
  original_text     VARCHAR2(2000 CHAR),                -- the user's own words
  readback          VARCHAR2(2000 CHAR),                -- plain English generated from `compiled`
  obsidian_path     VARCHAR2(1000 CHAR),                -- linked note (slice 2)
  created_at        TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
  updated_at        TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
  last_activity_at  TIMESTAMP WITH TIME ZONE
)
/
-- One live name per parent per user (NULL parent = top level). All three expressions are NULL for deleted rows,
-- so deleted projects never collide (Oracle doesn't index all-NULL keys).
CREATE UNIQUE INDEX projects_name_uk ON projects (
  CASE WHEN status <> 'deleted' THEN user_id END,
  CASE WHEN status <> 'deleted' THEN NVL(parent_id, 0) END,
  CASE WHEN status <> 'deleted' THEN LOWER(name) END)
/
CREATE INDEX projects_parent_ix ON projects (user_id, parent_id, status)
/
-- An email filed into a project. how: rule (a rule's project action), match (the project's own match), model
-- (Gemma picked the sub-project), thread (an earlier message of the thread is already here), manual (the user).
-- extracted_at: facts have been read from it (the pipeline extracts once per link).
CREATE TABLE project_links (
  id            NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id     NUMBER DEFAULT ON NULL TO_NUMBER(SYS_CONTEXT('EMAILD_CTX','TENANT_ID')) NOT NULL,
  user_id       NUMBER DEFAULT ON NULL TO_NUMBER(SYS_CONTEXT('EMAILD_CTX','USER_ID')) NOT NULL,
  project_id    NUMBER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  item_id       NUMBER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  thread_id     NUMBER REFERENCES threads(id) ON DELETE SET NULL,
  how           VARCHAR2(10) NOT NULL CHECK (how IN ('rule','match','model','thread','manual')),
  confidence    NUMBER,
  created_at    TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
  extracted_at  TIMESTAMP WITH TIME ZONE,
  CONSTRAINT project_links_uk UNIQUE (project_id, item_id)
)
/
CREATE INDEX project_links_thread_ix ON project_links (user_id, thread_id)
/
CREATE INDEX project_links_extract_ix ON project_links (user_id, extracted_at)
/
-- Facts pulled from filed emails. type: decision | ask (asked of the user) | commitment | deadline | open_question |
-- info. item_id is the citation; resolved_by_item_id the email that closed it. backfill: read from an email older
-- than 48 hours when filed, so it never counts as "new" in the brief.
CREATE TABLE project_facts (
  id                   NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id            NUMBER DEFAULT ON NULL TO_NUMBER(SYS_CONTEXT('EMAILD_CTX','TENANT_ID')) NOT NULL,
  user_id              NUMBER DEFAULT ON NULL TO_NUMBER(SYS_CONTEXT('EMAILD_CTX','USER_ID')) NOT NULL,
  project_id           NUMBER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  type                 VARCHAR2(14) NOT NULL
                       CHECK (type IN ('decision','ask','commitment','deadline','open_question','info')),
  text                 VARCHAR2(600 CHAR) NOT NULL,
  owner                VARCHAR2(200 CHAR),              -- "me" or a name
  due_at               TIMESTAMP WITH TIME ZONE,
  status               VARCHAR2(10) DEFAULT 'open' NOT NULL CHECK (status IN ('open','done','superseded')),
  confidence           NUMBER,
  item_id              NUMBER REFERENCES items(id) ON DELETE SET NULL,
  resolved_by_item_id  NUMBER REFERENCES items(id) ON DELETE SET NULL,
  backfill             BOOLEAN DEFAULT FALSE NOT NULL,
  created_at           TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
  updated_at           TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL
)
/
CREATE INDEX project_facts_open_ix ON project_facts (user_id, project_id, status)
/
-- The timeline: one ordered event log per project.
CREATE TABLE project_events (
  id           NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id    NUMBER DEFAULT ON NULL TO_NUMBER(SYS_CONTEXT('EMAILD_CTX','TENANT_ID')) NOT NULL,
  user_id      NUMBER DEFAULT ON NULL TO_NUMBER(SYS_CONTEXT('EMAILD_CTX','USER_ID')) NOT NULL,
  project_id   NUMBER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  kind         VARCHAR2(12) NOT NULL
               CHECK (kind IN ('created','email','fact','resolved','status','moved','note')),
  text         VARCHAR2(600 CHAR),
  item_id      NUMBER REFERENCES items(id) ON DELETE SET NULL,
  occurred_at  TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
  created_at   TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL
)
/
CREATE INDEX project_events_ix ON project_events (user_id, project_id, occurred_at)
/
-- Rare cross-umbrella relationships (a project has ONE parent; this is not a second one). Stored once, a_id < b_id.
CREATE TABLE project_related (
  id          NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id   NUMBER DEFAULT ON NULL TO_NUMBER(SYS_CONTEXT('EMAILD_CTX','TENANT_ID')) NOT NULL,
  user_id     NUMBER DEFAULT ON NULL TO_NUMBER(SYS_CONTEXT('EMAILD_CTX','USER_ID')) NOT NULL,
  a_id        NUMBER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  b_id        NUMBER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  created_at  TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
  CONSTRAINT project_related_ck CHECK (a_id < b_id),
  CONSTRAINT project_related_uk UNIQUE (a_id, b_id)
)
/
-- Sub-projects Gemma proposed while filing ("new: Uniform order"), never created automatically.
-- skey is "<parent_id>:<normalised name>"; a dismissed key is never suggested again.
CREATE TABLE project_suggestions (
  id          NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id   NUMBER DEFAULT ON NULL TO_NUMBER(SYS_CONTEXT('EMAILD_CTX','TENANT_ID')) NOT NULL,
  user_id     NUMBER DEFAULT ON NULL TO_NUMBER(SYS_CONTEXT('EMAILD_CTX','USER_ID')) NOT NULL,
  parent_id   NUMBER REFERENCES projects(id) ON DELETE CASCADE,
  name        VARCHAR2(200 CHAR) NOT NULL,
  evidence    JSON,                                   -- {"item_ids": [...], "count": n}
  skey        VARCHAR2(400) NOT NULL,
  status      VARCHAR2(10) DEFAULT 'open' NOT NULL CHECK (status IN ('open','accepted','dismissed')),
  project_id  NUMBER REFERENCES projects(id) ON DELETE SET NULL,
  created_at  TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
  acted_at    TIMESTAMP WITH TIME ZONE,
  CONSTRAINT project_suggestions_uk UNIQUE (user_id, skey)
)
/
-- Consumed-tracking: each email is considered once per umbrella (top-level project), whatever the outcome
-- (linked / none). A separate table so project_links only ever holds real links.
CREATE TABLE project_processed (
  tenant_id    NUMBER DEFAULT ON NULL TO_NUMBER(SYS_CONTEXT('EMAILD_CTX','TENANT_ID')) NOT NULL,
  user_id      NUMBER DEFAULT ON NULL TO_NUMBER(SYS_CONTEXT('EMAILD_CTX','USER_ID')) NOT NULL,
  umbrella_id  NUMBER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  item_id      NUMBER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  outcome      VARCHAR2(10) NOT NULL CHECK (outcome IN ('linked','none')),
  created_at   TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
  CONSTRAINT project_processed_pk PRIMARY KEY (umbrella_id, item_id)
)
/
BEGIN
  FOR t IN (SELECT column_value AS tname FROM TABLE(sys.odcivarchar2list('PROJECTS','PROJECT_LINKS',
                                                                         'PROJECT_FACTS','PROJECT_EVENTS',
                                                                         'PROJECT_RELATED','PROJECT_SUGGESTIONS',
                                                                         'PROJECT_PROCESSED'))) LOOP
    DBMS_RLS.ADD_POLICY(object_schema => USER, object_name => t.tname, policy_name => 'USER_SCOPE',
                        function_schema => USER, policy_function => 'VPD_USER_SCOPE',
                        statement_types => 'SELECT,INSERT,UPDATE,DELETE', update_check => TRUE,
                        policy_type => DBMS_RLS.DYNAMIC);
    EXECUTE IMMEDIATE 'GRANT SELECT, INSERT, UPDATE, DELETE ON ' || t.tname || ' TO email_app';
  END LOOP;
END;
/
