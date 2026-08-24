-- invoiceflow.db — the pipeline's system of record.
-- Design rationale: docs/schema.md. A drift test (tests/test_runstore.py)
-- asserts this file and that document produce the same database objects.
--
-- Amendments over the doc's first draft, mirrored back into it:
--   * runs.document_id is NULLable: a run that fails before its file can be
--     read (missing file, unsupported format) has no content to identify a
--     document by, but still deserves an audit row.
--   * v_run_summary therefore LEFT JOINs documents and pins is_latest /
--     document_run_no to 1 for document-less runs.

CREATE TABLE documents (
    id              INTEGER PRIMARY KEY,
    -- A document IS its content. Identity deliberately excludes the path, so
    -- the same invoice copied into an inbox/ folder is the same document and
    -- running it again registers as a re-run rather than as new work.
    content_sha256  TEXT    NOT NULL UNIQUE,  -- of raw_text, post-pdfplumber
    raw_text        TEXT    NOT NULL,         -- exactly what the Extractor saw
    file_format     TEXT    NOT NULL CHECK (file_format IN ('txt','json','csv','xml','pdf')),
    char_count      INTEGER NOT NULL,
    first_seen_path TEXT    NOT NULL,         -- where we first found it
    first_seen_at   TEXT    NOT NULL
) STRICT;

CREATE TABLE runs (
    id                  INTEGER PRIMARY KEY,
    run_id              TEXT    NOT NULL UNIQUE,        -- 'invoice_1001-023f0e1b'
    -- NULL only when the run failed before the file could be read: no content,
    -- no document identity — but the failure itself is still audit-worthy.
    document_id         INTEGER REFERENCES documents(id),
    source_path         TEXT    NOT NULL,               -- the path *this* run read
    started_at          TEXT    NOT NULL,
    finished_at         TEXT,
    duration_ms         INTEGER,
    llm_backend         TEXT    NOT NULL DEFAULT '',    -- 'grok-4.6'
    pipeline_revision   TEXT    NOT NULL DEFAULT '',    -- git describe --dirty
    final_status        TEXT    NOT NULL
        CHECK (final_status IN ('paid','rejected','needs_review','duplicate','failed')),
    -- What the system decided, and who actually decided it. Splitting these
    -- two is the fix for overrides silently rewriting the agent's reasoning.
    decision_status     TEXT    CHECK (decision_status IN ('approved','rejected','needs_review')),
    decision_source     TEXT    CHECK (decision_source IN
                              ('agent','hard_rule','critic_escalation','critic_exhausted','human')),
    quarantine_reason   TEXT    NOT NULL DEFAULT '',
    error               TEXT    NOT NULL DEFAULT '',
    langsmith_trace_url TEXT    NOT NULL DEFAULT ''     -- deep link, when tracing was on
) STRICT;

CREATE TABLE agent_invocations (
    id           INTEGER PRIMARY KEY,
    run_id       INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    seq          INTEGER NOT NULL,              -- order within the run
    node         TEXT    NOT NULL CHECK (node  IN ('ingest','validate','decide','critique')),
    agent        TEXT    NOT NULL CHECK (agent IN ('extractor','validator','approver','critic')),
    round_no     INTEGER NOT NULL DEFAULT 1,    -- reflection round
    -- The reflection loop's causality, as a self-FK rather than a string: the
    -- Critic that said 'revise' is literally what caused the next Approver
    -- draft. NULL means the turn was entered from a graph edge.
    triggered_by INTEGER REFERENCES agent_invocations(id),
    outcome      TEXT    NOT NULL CHECK (outcome IN ('ok','retried','failed')),
    error        TEXT    NOT NULL DEFAULT '',
    started_at   TEXT    NOT NULL,
    duration_ms  INTEGER,                       -- wall clock: model time + tool time
    UNIQUE (run_id, seq)
) STRICT;

CREATE TABLE invoices (
    id             INTEGER PRIMARY KEY,
    run_id         INTEGER NOT NULL UNIQUE REFERENCES runs(id) ON DELETE CASCADE,
    invocation_id  INTEGER NOT NULL UNIQUE REFERENCES agent_invocations(id) ON DELETE CASCADE,
    invoice_number TEXT    NOT NULL,
    vendor         TEXT    NOT NULL DEFAULT '',
    invoice_date   TEXT,
    due_date       TEXT,
    due_date_raw   TEXT,                       -- 'yesterday' etc. when unparseable
    subtotal       REAL,
    tax_amount     REAL,
    extra_charges  REAL    NOT NULL DEFAULT 0,
    total          REAL,
    currency       TEXT    NOT NULL DEFAULT 'USD',
    payment_terms  TEXT    NOT NULL DEFAULT '',
    notes          TEXT    NOT NULL DEFAULT '',
    content_hash   TEXT    NOT NULL            -- Invoice.content_hash(), cross-format dupes
) STRICT;

CREATE TABLE invoice_line_items (
    id         INTEGER PRIMARY KEY,
    invoice_id INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    line_no    INTEGER NOT NULL,
    item       TEXT    NOT NULL,
    quantity   INTEGER NOT NULL,               -- negatives kept: they are evidence
    unit_price REAL,
    line_total REAL,
    note       TEXT,
    UNIQUE (invoice_id, line_no)
) STRICT;

-- Self-correction loop #1, previously thrown away. One row per attempt that
-- failed schema or sanity checks, holding the feedback that went into the
-- next prompt. A clean first pass leaves this table empty.
CREATE TABLE extraction_attempts (
    id            INTEGER PRIMARY KEY,
    invocation_id INTEGER NOT NULL REFERENCES agent_invocations(id) ON DELETE CASCADE,
    attempt_no    INTEGER NOT NULL,            -- 1-based
    problems      TEXT    NOT NULL,            -- verbatim, as fed back to the model
    UNIQUE (invocation_id, attempt_no)
) STRICT;

CREATE TABLE issue_codes (
    code        TEXT PRIMARY KEY,             -- seeded from IssueCode
    category    TEXT NOT NULL CHECK (category IN
                    ('inventory','arithmetic','integrity','duplicate','prompt_safety','agent')),
    description TEXT NOT NULL DEFAULT ''
) STRICT;

CREATE TABLE validation_reports (
    id            INTEGER PRIMARY KEY,
    run_id        INTEGER NOT NULL UNIQUE REFERENCES runs(id) ON DELETE CASCADE,
    invocation_id INTEGER NOT NULL UNIQUE REFERENCES agent_invocations(id) ON DELETE CASCADE,
    summary       TEXT    NOT NULL DEFAULT ''  -- the Validator's prose read
) STRICT;

CREATE TABLE validation_tool_runs (
    id         INTEGER PRIMARY KEY,
    report_id  INTEGER NOT NULL REFERENCES validation_reports(id) ON DELETE CASCADE,
    seq        INTEGER NOT NULL,
    tool_name  TEXT    NOT NULL,
    -- 'agent' = the Validator chose to call it; 'safety_net' = agents.py ran it
    -- because the agent skipped it. Makes "how often does the agent miss a
    -- check?" a query, which is the honest measure of the tool loop's value.
    invoked_by TEXT    NOT NULL CHECK (invoked_by IN ('agent','safety_net')),
    UNIQUE (report_id, tool_name)
) STRICT;

CREATE TABLE validation_issues (
    id        INTEGER PRIMARY KEY,
    report_id INTEGER NOT NULL REFERENCES validation_reports(id) ON DELETE CASCADE,
    seq       INTEGER NOT NULL,
    code      TEXT    NOT NULL REFERENCES issue_codes(code),
    severity  TEXT    NOT NULL CHECK (severity IN ('info','warning','critical')),
    detail    TEXT    NOT NULL,
    -- What the finding is *about*: the item, the currency, the invoice
    -- number. Empty when it is about the vendor's practice as such —
    -- arithmetic drift and dating quirks belong to the vendor, not to any
    -- one value on the page. This is the key precedent is matched on, so a
    -- finding can be compared with history without parsing `detail`'s prose.
    subject   TEXT    NOT NULL DEFAULT '',
    -- 'tool' issues carry authority over routing; 'agent' issues are advisory
    -- (ValidatorSummary demotes them). Today that distinction survives only as
    -- the code 'agent_observation'; here it is a column you can filter on.
    origin    TEXT    NOT NULL CHECK (origin IN ('tool','agent')),
    tool_name TEXT                            -- NULL for agent observations
) STRICT;

CREATE TABLE rule_evaluations (
    id                 INTEGER PRIMARY KEY,
    run_id             INTEGER NOT NULL UNIQUE REFERENCES runs(id) ON DELETE CASCADE,
    must_reject        INTEGER NOT NULL CHECK (must_reject       IN (0,1)),
    must_review        INTEGER NOT NULL CHECK (must_review       IN (0,1)),
    requires_scrutiny  INTEGER NOT NULL CHECK (requires_scrutiny IN (0,1)),
    -- The policy this run was judged under. Threshold changes are inevitable;
    -- without this, historical decisions become unexplainable.
    scrutiny_threshold REAL    NOT NULL
) STRICT;

-- Normalized per D9: `kind` is a real attribute, and "top rejection reasons"
-- is a headline query for the business write-up.
CREATE TABLE rule_reasons (
    id                 INTEGER PRIMARY KEY,
    rule_evaluation_id INTEGER NOT NULL REFERENCES rule_evaluations(id) ON DELETE CASCADE,
    kind               TEXT    NOT NULL CHECK (kind IN
                           ('reject','review','scrutiny','advisory','precedent')),
    reason             TEXT    NOT NULL
) STRICT;

CREATE TABLE approver_decisions (
    id            INTEGER PRIMARY KEY,
    invocation_id INTEGER NOT NULL UNIQUE REFERENCES agent_invocations(id) ON DELETE CASCADE,
    status        TEXT    NOT NULL CHECK (status IN ('approved','rejected','needs_review')),
    reasoning     TEXT    NOT NULL,            -- verbatim; never appended to
    -- JSON per D9: bare strings, no attributes, rendered as a bullet list.
    -- Still queryable via json_each(risk_factors) if that ever changes.
    risk_factors  TEXT    NOT NULL DEFAULT '[]' CHECK (json_valid(risk_factors))
) STRICT;

CREATE TABLE critic_reviews (
    id            INTEGER PRIMARY KEY,
    invocation_id INTEGER NOT NULL UNIQUE REFERENCES agent_invocations(id) ON DELETE CASCADE,
    verdict       TEXT    NOT NULL CHECK (verdict IN ('affirm','revise','escalate')),
    feedback      TEXT    NOT NULL
) STRICT;

-- What the *system* did about what the agents said. Not an agent turn, so it
-- hangs off the run: hard rules and loop exhaustion are deterministic.
CREATE TABLE decision_overrides (
    id            INTEGER PRIMARY KEY,
    run_id        INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    -- The Approver turn whose decision this overrode. round_no is reachable
    -- through it, so it is not stored twice.
    invocation_id INTEGER NOT NULL REFERENCES agent_invocations(id) ON DELETE CASCADE,
    kind        TEXT    NOT NULL CHECK (kind IN
                    ('hard_rule_review','hard_rule_reject','critic_escalation','critic_exhausted')),
    from_status TEXT    NOT NULL,
    to_status   TEXT    NOT NULL,
    reasoning   TEXT    NOT NULL,
    created_at  TEXT    NOT NULL
) STRICT;

CREATE TABLE payments (
    id        INTEGER PRIMARY KEY,
    run_id    INTEGER NOT NULL UNIQUE REFERENCES runs(id) ON DELETE CASCADE,
    status    TEXT    NOT NULL CHECK (status IN ('success','skipped_already_paid')),
    vendor    TEXT    NOT NULL,
    amount    REAL    NOT NULL,
    currency  TEXT    NOT NULL DEFAULT 'USD',
    reference TEXT    NOT NULL DEFAULT '',
    paid_at   TEXT    NOT NULL
) STRICT;

-- One row per round-trip to the model. Lives here, not only in LangSmith:
-- tracing is opt-in and off by default, so this is the only per-call record a
-- normal run leaves behind (D8).
CREATE TABLE llm_calls (
    id                  INTEGER PRIMARY KEY,
    -- No stage and no run_id: the spine already knows which agent ran this
    -- call, at which node, in which round, for which run.
    invocation_id       INTEGER NOT NULL REFERENCES agent_invocations(id) ON DELETE CASCADE,
    seq                 INTEGER NOT NULL,      -- call order within the turn
    model               TEXT    NOT NULL,
    input_tokens        INTEGER NOT NULL DEFAULT 0,
    cached_input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens       INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens    INTEGER NOT NULL DEFAULT 0,
    total_tokens        INTEGER NOT NULL DEFAULT 0,
    tool_calls          INTEGER NOT NULL DEFAULT 0,
    latency_ms          INTEGER,               -- model time alone
    cost_usd            REAL,                  -- snapshot under the day's prices
    langsmith_run_id    TEXT,                  -- drill-down when tracing was on
    started_at          TEXT    NOT NULL,
    error               TEXT    NOT NULL DEFAULT '',
    UNIQUE (invocation_id, seq)
) STRICT;

CREATE TABLE model_pricing (
    model                     TEXT NOT NULL,
    effective_from            TEXT NOT NULL,
    input_usd_per_mtok        REAL NOT NULL,
    cached_input_usd_per_mtok REAL,
    output_usd_per_mtok       REAL NOT NULL,
    PRIMARY KEY (model, effective_from)
) STRICT;

CREATE TABLE trace_events (
    id     INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    seq    INTEGER NOT NULL,
    stage  TEXT    NOT NULL,
    event  TEXT    NOT NULL,
    detail TEXT    NOT NULL DEFAULT '',
    at     TEXT    NOT NULL,                  -- gives per-stage timings
    UNIQUE (run_id, seq)
) STRICT;

-- The one mutable state table: current status per invoice number.
-- Replaces processed_invoices; same semantics, now FK'd to the run.
CREATE TABLE invoice_registry (
    invoice_number TEXT PRIMARY KEY,
    content_hash   TEXT    NOT NULL,
    vendor         TEXT    NOT NULL DEFAULT '',
    total          REAL,
    final_status   TEXT    NOT NULL
        CHECK (final_status IN ('paid','rejected','needs_review','duplicate','failed')),
    last_run_id    INTEGER REFERENCES runs(id),
    processed_at   TEXT    NOT NULL
) STRICT;

-- Many reviews per run: a run can be confirmed, reopened and overturned, and
-- each of those is a fact worth keeping. "Unchecked" is the absence of any row.
CREATE TABLE human_reviews (
    id          INTEGER PRIMARY KEY,
    run_id      INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    reviewed_at TEXT    NOT NULL,
    reviewer    TEXT    NOT NULL DEFAULT 'dashboard',
    action      TEXT    NOT NULL CHECK (action IN
                    ('confirm','override_approve','override_reject')),
    from_status TEXT    NOT NULL,
    to_status   TEXT    NOT NULL,
    note        TEXT    NOT NULL DEFAULT ''
) STRICT;

-- Which past human decisions authorized this run's automatic one. Written for
-- every finding precedent was consulted on, released or not: a refusal to
-- release is as much a fact about the policy as a release is.
--
-- `terms` holds the burden/support breakdown rather than only the two totals.
-- That is deliberate and forward-looking: with the outcome in `human_reviews`,
-- these rows are a labelled training set for the day the hand-set weights are
-- replaced by a fitted model.
CREATE TABLE precedent_citations (
    id            INTEGER PRIMARY KEY,
    run_id        INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    code          TEXT    NOT NULL REFERENCES issue_codes(code),
    -- The question, exactly as validation_issues stated it.
    subject       TEXT    NOT NULL DEFAULT '',
    vendor        TEXT    NOT NULL DEFAULT '',
    cases         INTEGER NOT NULL DEFAULT 0,   -- prior human approvals on this key
    rejections    INTEGER NOT NULL DEFAULT 0,   -- any at all zeroes the support
    burden        REAL    NOT NULL,             -- what had to be proven
    support       REAL    NOT NULL,             -- what history supplied
    released      INTEGER NOT NULL CHECK (released IN (0,1)),
    -- Set when something barred release outright, whatever the arithmetic said
    -- (over the scrutiny threshold, a forged prompt fence, a non-releasable code).
    blocked_by    TEXT    NOT NULL DEFAULT '',
    cited_run_ids TEXT    NOT NULL DEFAULT '[]' CHECK (json_valid(cited_run_ids)),
    terms         TEXT    NOT NULL DEFAULT '{}' CHECK (json_valid(terms)),
    UNIQUE (run_id, code, subject)
) STRICT;

-- One row per file a person handed the dashboard. Deliberately not a column on
-- `runs`: an item exists before any run does, and one that never reaches a run
-- at all — an unreadable PDF, a missing API key — still has to be visible and
-- explainable to whoever uploaded it. `runs` records work done; this records
-- work asked for.
CREATE TABLE inbox_items (
    id             INTEGER PRIMARY KEY,
    filename       TEXT    NOT NULL,          -- exactly what the browser sent
    -- Unique by construction: every upload gets its own directory, so the
    -- vendor's filename survives collisions intact and still reads well inside
    -- run_id, which pipeline.py builds from the path stem.
    stored_path    TEXT    NOT NULL UNIQUE,
    file_format    TEXT    NOT NULL CHECK (file_format IN ('txt','json','csv','xml','pdf')),
    byte_size      INTEGER NOT NULL,
    -- Of the loaded TEXT, not of the bytes: that is what documents.content_sha256
    -- keys on, so the same invoice re-exported as a fresh PDF is still recognised
    -- as the same document. Not a foreign key — the documents row is written by
    -- the ingest node, which has not run yet when this row is created.
    content_sha256 TEXT    NOT NULL,
    -- How many runs this exact content already had when it was uploaded. >0 is
    -- what the upload dialog warns about, before six Grok calls are spent.
    prior_runs     INTEGER NOT NULL DEFAULT 0,
    source         TEXT    NOT NULL DEFAULT 'upload'
        CHECK (source IN ('upload','samples')),
    -- 'processed' means a run happened and reached a verdict — NOT that the
    -- verdict was good. A run that ended `failed` is a processed item with an
    -- honest answer, which is a different fact from an item we never managed to
    -- start at all. That one is 'failed' here.
    state          TEXT    NOT NULL DEFAULT 'queued'
        CHECK (state IN ('queued','processing','processed','failed')),
    stage          TEXT    NOT NULL DEFAULT '',  -- live graph node while processing
    run_id         INTEGER REFERENCES runs(id),
    error          TEXT    NOT NULL DEFAULT '',
    enqueued_at    TEXT    NOT NULL,
    started_at     TEXT,
    finished_at    TEXT,
    dismissed_at   TEXT                        -- cleared from the list, never deleted
) STRICT;

-- ---------------------------------------------------------------------------
-- Views
-- ---------------------------------------------------------------------------

CREATE VIEW v_approval_rounds AS
SELECT ai.run_id, ai.round_no,
       d.status       AS decision_status,
       d.reasoning    AS decision_reasoning,
       d.risk_factors AS risk_factors_json,
       c.verdict      AS critic_verdict,
       c.feedback     AS critic_feedback,
       ai.started_at
FROM agent_invocations ai
JOIN      approver_decisions d  ON d.invocation_id = ai.id
LEFT JOIN agent_invocations  ci ON ci.triggered_by = ai.id AND ci.agent = 'critic'
LEFT JOIN critic_reviews     c  ON c.invocation_id = ci.id
WHERE ai.agent = 'approver';

CREATE VIEW v_run_summary AS
SELECT r.run_id, r.started_at, r.duration_ms, r.final_status,
       r.decision_status, r.decision_source,
       r.source_path, d.file_format,
       i.invoice_number, i.vendor, i.total, i.currency,
       -- D10: superseded runs stay visible and stay in the analytics.
       -- Document-less runs (load failures) count as their own latest.
       CASE WHEN r.document_id IS NULL THEN 1 ELSE
         (r.id = (SELECT MAX(r2.id) FROM runs r2 WHERE r2.document_id = r.document_id))
       END AS is_latest,
       -- >1 means this document has been through the pipeline before.
       CASE WHEN r.document_id IS NULL THEN 1 ELSE
         (SELECT COUNT(*) FROM runs r3
          WHERE r3.document_id = r.document_id AND r3.id <= r.id)
       END AS document_run_no,
       COALESCE(iss.issue_count, 0)        AS issue_count,
       COALESCE(iss.critical_count, 0)     AS critical_count,
       COALESCE(tel.calls, 0)              AS llm_calls,
       COALESCE(tel.total_tokens, 0)       AS total_tokens,
       ROUND(COALESCE(tel.cost_usd, 0), 4) AS cost_usd,
       hr.last_reviewed_at                 AS human_reviewed_at
FROM runs r
LEFT JOIN documents d ON d.id = r.document_id
LEFT JOIN invoices  i ON i.run_id = r.id
-- Pre-aggregated, not joined directly: issues and calls are both one-to-many
-- off the run, and joining them side by side would multiply the cost sum by
-- the issue count.
LEFT JOIN (
    SELECT vr.run_id,
           COUNT(*)                      AS issue_count,
           SUM(vi.severity = 'critical') AS critical_count
    FROM validation_reports vr
    JOIN validation_issues  vi ON vi.report_id = vr.id
    GROUP BY vr.run_id
) iss ON iss.run_id = r.id
LEFT JOIN (
    SELECT ai.run_id, COUNT(*) AS calls,
           SUM(lc.total_tokens) AS total_tokens, SUM(lc.cost_usd) AS cost_usd
    FROM llm_calls lc
    JOIN agent_invocations ai ON ai.id = lc.invocation_id
    GROUP BY ai.run_id
) tel ON tel.run_id = r.id
LEFT JOIN (
    SELECT run_id, MAX(reviewed_at) AS last_reviewed_at
    FROM human_reviews GROUP BY run_id
) hr ON hr.run_id = r.id;

-- What the dashboard shows by default.
CREATE VIEW v_current_runs AS
SELECT * FROM v_run_summary WHERE is_latest = 1;

-- The escalation queue, biggest exposure first.
CREATE VIEW v_review_queue AS
SELECT * FROM v_current_runs
WHERE final_status = 'needs_review' AND human_reviewed_at IS NULL
ORDER BY total DESC;

-- Every finding a *person* settled, and how. The single definition of "history
-- has an answer to this question", read by precedent.py.
--
-- Note what the inner joins do rather than a WHERE clause: a run with no
-- human_reviews row simply is not here. That is the compounding guard expressed
-- as shape — an automatically approved invoice can never become the evidence
-- for the next automatic approval, because it has nothing to join to.
CREATE VIEW v_review_precedent AS
SELECT vi.code, vi.subject, i.vendor,
       r.run_id, i.invoice_number, i.total, i.currency, vi.detail,
       hr.action, hr.to_status, hr.reviewed_at, hr.note
FROM validation_issues  vi
JOIN validation_reports vr ON vr.id = vi.report_id
JOIN runs               r  ON r.id  = vr.run_id
JOIN invoices           i  ON i.run_id = r.id
-- The latest review only: a run can be confirmed, reopened and overturned, and
-- only where it finally landed is what the person actually decided.
JOIN human_reviews      hr ON hr.id =
     (SELECT MAX(h.id) FROM human_reviews h WHERE h.run_id = r.id)
-- Tool-authored findings only. ValidatorSummary already demotes agent-authored
-- issues and clears their subject; this is the second lock on a key no model
-- may mint for itself.
WHERE vi.origin = 'tool';

-- What the pipeline has learned, per open question. Drives the Learning tab and
-- answers "which reviews is the system now acting on by itself?" in one query.
CREATE VIEW v_precedent_learning AS
SELECT code, subject, vendor,
       SUM(to_status = 'paid')     AS approvals,
       SUM(to_status = 'rejected') AS rejections,
       COUNT(*)                    AS decisions,
       MAX(reviewed_at)            AS last_reviewed_at,
       -- The largest sum a person has signed off on this key. Precedent never
       -- reaches past it by much, so it reads as the standing's ceiling.
       MAX(CASE WHEN to_status = 'paid' THEN total END) AS largest_approved,
       GROUP_CONCAT(invoice_number, ', ') AS invoices
FROM v_review_precedent
GROUP BY code, subject, vendor
ORDER BY approvals DESC;

-- What the Inbox tab reads: every upload with the outcome of the run it
-- produced folded in. LEFT JOINs throughout — an item that has not run yet is
-- the normal case, not a missing row. Dismissed rows drop out here and stay in
-- the table.
CREATE VIEW v_inbox AS
SELECT ib.id, ib.filename, ib.file_format, ib.byte_size, ib.source, ib.state,
       ib.stage, ib.error, ib.prior_runs, ib.enqueued_at, ib.started_at, ib.finished_at,
       r.run_id, s.final_status, s.invoice_number, s.vendor, s.total, s.currency,
       s.duration_ms, s.cost_usd, s.issue_count, s.document_run_no
FROM inbox_items ib
LEFT JOIN runs          r ON r.id = ib.run_id
LEFT JOIN v_run_summary s ON s.run_id = r.run_id
WHERE ib.dismissed_at IS NULL;

-- Business impact: what actually goes wrong, most common first.
CREATE VIEW v_issue_frequency AS
SELECT ic.category, vi.code, vi.severity, vi.origin, COUNT(*) AS occurrences
FROM validation_issues vi
JOIN issue_codes ic ON ic.code = vi.code
GROUP BY ic.category, vi.code, vi.severity, vi.origin
ORDER BY occurrences DESC;

-- Where the money goes. Turn stats and call stats are aggregated separately
-- and then joined: counting turns across the call fan-out would inflate both
-- the turn count and the average duration.
CREATE VIEW v_cost_by_agent AS
SELECT t.agent, t.turns, t.avg_duration_ms,
       COALESCE(c.calls, 0)              AS calls,
       COALESCE(c.tokens, 0)             AS tokens,
       ROUND(COALESCE(c.cost_usd, 0), 4) AS cost_usd
FROM (
    SELECT agent, COUNT(*) AS turns, ROUND(AVG(duration_ms)) AS avg_duration_ms
    FROM agent_invocations GROUP BY agent
) t
LEFT JOIN (
    SELECT ai.agent, COUNT(*) AS calls,
           SUM(lc.total_tokens) AS tokens, SUM(lc.cost_usd) AS cost_usd
    FROM llm_calls lc
    JOIN agent_invocations ai ON ai.id = lc.invocation_id
    GROUP BY ai.agent
) c ON c.agent = t.agent
ORDER BY cost_usd DESC;

-- Accidental reprocessing: the same file, run more than once.
CREATE VIEW v_reprocessed_documents AS
SELECT d.first_seen_path, COUNT(r.id) AS run_count,
       COUNT(DISTINCT r.source_path)          AS distinct_paths,
       MIN(r.started_at) AS first_run_at, MAX(r.started_at) AS last_run_at,
       GROUP_CONCAT(r.final_status, ' -> ')   AS statuses
FROM documents d
JOIN runs r ON r.document_id = d.id
GROUP BY d.id
HAVING COUNT(r.id) > 1;

-- Both self-correction loops, in one place, for the write-up.
CREATE VIEW v_self_correction AS
SELECT r.run_id,
       COALESCE(ex.retries, 0)  AS extraction_retries,
       COALESCE(rev.redrafts,0) AS approver_redrafts
FROM runs r
LEFT JOIN (
    SELECT ai.run_id, COUNT(*) AS retries
    FROM extraction_attempts ea
    JOIN agent_invocations ai ON ai.id = ea.invocation_id
    GROUP BY ai.run_id
) ex ON ex.run_id = r.id
LEFT JOIN (
    SELECT run_id, COUNT(*) AS redrafts
    FROM agent_invocations
    WHERE agent = 'approver' AND triggered_by IS NOT NULL
    GROUP BY run_id
) rev ON rev.run_id = r.id;

-- ---------------------------------------------------------------------------
-- Indexes (only what UNIQUE does not already cover)
-- ---------------------------------------------------------------------------

CREATE INDEX idx_runs_document        ON runs(document_id);
CREATE INDEX idx_runs_status_started  ON runs(final_status, started_at DESC);
CREATE INDEX idx_invocations_trigger  ON agent_invocations(triggered_by);
CREATE INDEX idx_invocations_agent    ON agent_invocations(agent, round_no);
CREATE INDEX idx_invoices_number      ON invoices(invoice_number);
CREATE INDEX idx_invoices_hash        ON invoices(content_hash);
CREATE INDEX idx_issues_report        ON validation_issues(report_id);
CREATE INDEX idx_issues_code          ON validation_issues(code, severity);
-- The precedent lookup's driving predicate: one question, across all history.
CREATE INDEX idx_issues_subject       ON validation_issues(code, subject);
CREATE INDEX idx_overrides_run        ON decision_overrides(run_id);
CREATE INDEX idx_overrides_invocation ON decision_overrides(invocation_id);
CREATE INDEX idx_human_reviews_run    ON human_reviews(run_id);
CREATE INDEX idx_inbox_state          ON inbox_items(state, id);
