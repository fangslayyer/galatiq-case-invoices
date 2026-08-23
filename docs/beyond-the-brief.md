# Beyond the brief

Talking points for the walkthrough. [CASE.md](CASE.md) defines four stages and a
short acceptance table; everything below is an addition, with the reasoning that
motivated it. Status is marked honestly — **shipped** items run today and are
covered by the test suite, **proposed** items are designed but not built.

The grouping follows the brief's own evaluation criteria.

---

## Agentic sophistication

### 1. The model cannot seize control of the pipeline — shipped
The Validator agent may report anything it noticed, but `ValidatorSummary`
rewrites every agent-authored issue to `agent_observation` and downgrades
`critical` to `warning` before it reaches the graph
([models.py](../src/invoiceflow/models.py) `_demote_to_observations`).

**Why it matters:** an unclamped summary claiming `duplicate_invoice/critical`
would route the run straight to `record`, and *any* critical code trips
`must_reject`. That is control flow authored by the text being judged. The
agent keeps its voice and loses its authority.

### 2. Prompt-injection quarantine, before any model sees the document — shipped
A document that forges the pipeline's own prompt fences is quarantined at
ingestion and never shown to an LLM at all
([graph.py](../src/invoiceflow/graph.py) `_prompt_safety_gate`).

**Why it matters:** the Extractor is itself an LLM, so "validate it first" is too
late — the attack lands during extraction. It is also never auto-rejected:
deciding whether a forged fence is an attack or an OCR artifact is the one
judgement no agent here is fit to make, so it goes to a human.

### 3. Hard rules outrank the agents, and `must_review` outranks `must_reject` — shipped
Deterministic constraints are computed before the Approver runs, and the graph
enforces them regardless of what the agents concluded
([rules.py](../src/invoiceflow/rules.py), [graph.py](../src/invoiceflow/graph.py)).

**Why it matters:** a rejection is an accusation. Where the rules also say a fact
could not be established (no readable total, say), the accusation is exactly what
a person should confirm before it stands — so the invoice lands in the review
queue rather than being auto-rejected on incomplete evidence.

### 4. A safety net under the Validator's tool choice — shipped
The agent decides which checks to call; the pipeline then runs any it skipped
([agents.py](../src/invoiceflow/agents.py)).

**Why it matters:** tool use gives the reasoning trace, not control over whether
business-critical checks happen. In a payments system those are different jobs.
The proposed schema records `invoked_by` per tool, which turns "how often does
the agent miss a check?" into a query rather than an opinion.

### 5. Two self-correction loops, of different kinds — shipped
The Extractor's is *in-agent* — schema and sanity failures are fed back into the
next prompt. The Approver ↔ Critic loop is a real conditional edge in LangGraph,
routing `critique` back to `decide`.

**Why it matters:** the brief asks for "a reflection or critique loop"; showing
both a retry loop and an adversarial reviewer, and being precise about which one
is a graph edge, is the difference between using the framework and understanding it.

---

## Functionality

### 6. Duplicate and revision detection across formats — shipped
Not mentioned anywhere in the brief. A canonical `content_hash` over invoice
number, vendor, total and line items identifies the same invoice whether it
arrived as PDF, CSV or text. Same number + same content is an exact duplicate;
same number + different content is a revision
([validation.py](../src/invoiceflow/validation.py) `check_duplicate`).

**Why it matters:** duplicate payment is one of the most common real AP losses,
and a resubmission in a new format is precisely what slips past a human reviewer.
`invoice_1011.pdf` then `invoice_1011.txt` demonstrates it.

### 7. Payment idempotency, enforced at the payer — shipped
`execute_payment` refuses to pay an invoice the registry already records as paid,
independently of how the run reached that point
([payment.py](../src/invoiceflow/payment.py)).

**Why it matters:** defence in depth. The graph should never route a duplicate to
payment; the payer assumes it might anyway.

### 8. Five input formats — shipped
`.txt`, `.json`, `.csv`, `.xml`, `.pdf`. The brief asks for PDFs and text files.

---

## UI/UX

### 9. A review dashboard with an escalation queue — shipped
Streamlit app ([ui/app.py](../ui/app.py)): queue ordered for human attention,
full agent trace per run, and confirm/override actions that stamp
`human_reviewed_at`.

**Why it matters:** confirming is not a no-op — it is how an auto-rejection stops
counting as something nobody has checked. The queue is the product for the
finance team; the CLI is the product for engineers.

---

## Code quality & observability

### 10. LangSmith tracing, opt-in and development-only — shipped
Off by default, behind `LANGSMITH_TRACING=true`, forced off in the test suite,
and the CLI prints a banner whenever it is live. The gate reads all four
environment variables the tracer itself consults, so a traced run can never look
untraced ([config.py](../src/invoiceflow/config.py)).

**Why it matters:** the brief allows no external API but Grok. Tracing sends
prompts and invoice contents to a third party, so it is a debugging tool that
must be impossible to leave on by accident — and the `langsmith` client is
declared as a dev dependency to say so in the manifest.

### 11. The graph diagram cannot lie — shipped
`docs/graph.png` is rendered from the compiled LangGraph. Rendering goes through
mermaid.ink, so it is a manual step (`--export-graph`), and a test compares the
committed mermaid source against the topology as compiled today.

**Why it matters:** same principle as above — no external call on the hot path —
without giving up the guarantee that the picture matches the code.

### 12. 110 offline tests, plus a separate live suite — shipped
The offline suite runs every sample file through the full graph with extraction
answered from recorded ground-truth fixtures; the live suite verifies real Grok
honours that contract. No API key needed to run the tests.

**Why it matters:** it makes the whole pipeline testable without spending money
or depending on a model's mood, while still checking the model against the
documented contract.

---

### 13. Relational system of record — shipped ([schema.md](schema.md))
21 tables and 8 views (`invoiceflow.db`) replacing `results/*.json` as the
system of record; JSON survives as a derived export (`--export-json`). Captures
three things the JSON silently dropped: the rule constraints that forced an
outcome, the Extractor's retry feedback, and the agent's original decision when
anything overrides it. A drift test keeps the design doc and the shipped DDL
the same database, and `begin_run` writes a pessimistic `failed` row up front,
so even a crash leaves an honest audit trail.

### 14. Cost and token telemetry per invoice — shipped
Every LLM round-trip is recorded locally (`llm_calls`: tokens, reasoning
tokens, latency, model) under the agent turn that made it — not only in
LangSmith, which is off by default. The CLI prints usage per run and per batch;
`v_cost_by_agent` answers "where does the money go". Dollar cost appears once
`model_pricing` holds your rates — never invented from a missing price.

### 15. Re-run detection at the document level — shipped
The duplicate check is keyed on the *extracted* invoice number, so it only
fires after the Extractor has been paid for. Documents are now identified by
content hash at load time, which separates "a vendor resubmitted an invoice"
from "an operator ran the same file twice" — different human responses.
Surfaced as a non-blocking CLI notice and `v_reprocessed_documents`; a
confirmation prompt would break `--all` and scripting, and double payment is
already prevented downstream.

### 16. Human reviews as first-class records — shipped
A dashboard action never edits what the agents wrote. It lands as a
`human_reviews` row (confirm / override), the effective status is derived, and
"how often do humans overturn the Approver?" is one query.

---

## Proposed — designed, not built

### 17. Short-circuit an exact re-run before extraction
Document identity is reliable now, so re-running an unchanged file could skip
straight to the recorded outcome, saving all six Grok calls.

---

## Deliberate scope cuts

Named in the README, repeated here because "what we did not build, and why" is
part of the same story: stock is validated but not decremented (reservation
semantics belong to the real inventory system); vendor master-data checks are out
of scope, with the `unknown vendor` fraud signal as the hook they would attach to;
email ingestion is simulated by an email-style fixture, with an IMAP poller
slotting in front of the loader unchanged.
