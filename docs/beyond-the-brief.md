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

Nine worked examples — seven attacks, one clean control and one honest false
positive — are in [data/demo/injection/README.md](../data/demo/injection/README.md),
with the outcome of each recorded from a real run. The set makes the narrower
claim the architecture actually supports: on the forged-fence document no model
was consulted at all, and on the other six no model had the authority to change
the outcome.

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

### 12. 268 offline tests, plus a separate live suite — shipped
The offline suite runs every sample file through the full graph with extraction
answered from recorded ground-truth fixtures; the live suite verifies real Grok
honours that contract. No API key needed to run the tests.

**Why it matters:** it makes the whole pipeline testable without spending money
or depending on a model's mood, while still checking the model against the
documented contract.

---

### 13. Relational system of record — shipped ([schema.md](schema.md))
23 tables and 11 views (`invoiceflow.db`) replacing `results/*.json` as the
system of record; JSON survives as a derived export (`--export-json`). Captures
three things the JSON silently dropped: the rule constraints that forced an
outcome, the Extractor's retry feedback, and the agent's original decision when
anything overrides it. A drift test keeps the design doc and the shipped DDL
the same database, and `begin_run` writes a pessimistic `failed` row up front,
so even a crash leaves an honest audit trail.

The schema also *grows* in place. Opening a database created by an older
revision adds whatever objects and columns it lacks, and rebuilds a table whose
CHECK has been widened — so a change reaches the databases that already exist
rather than dying on the first real invoice that needs it. Additive only, and
never a substitute for a migration tool; the point is simply that nobody's audit
trail should be the price of an upgrade.

### 14. Cost and token telemetry per invoice — shipped
Every LLM round-trip is recorded locally (`llm_calls`: tokens, reasoning
tokens, latency, model) under the agent turn that made it — not only in
LangSmith, which is off by default. The CLI prints usage per run and per batch;
`v_cost_by_agent` answers "where does the money go". Dollar cost appears once
`model_pricing` holds your rates — never invented from a missing price.

The backend's own rate is seeded on init, so it is on file before the first run
rather than after somebody notices the dashboard showing a dash: grok-4.6 at
$2.00 / $0.50 cached / $6.00 per million tokens, xAI's published price
([docs.x.ai/docs/models](https://docs.x.ai/docs/models), read 2026-08-24). It is
a floor, not a claim about history — a real rate change is a new row carrying
the date it took effect, which is what `effective_from` is for, and a rate set
by hand is never overwritten by the seed. `--init-db` also fills in calls
recorded before any price was known; a cost already snapshotted is left alone,
because that one is a record of what was billed.

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

### 17. Warn on an exact re-run before extraction — shipped, half of it
Document identity is reliable, so the upload dialog hashes a file the moment it
arrives — the same SHA-256 of loaded text that `documents` keys on — and says
"these exact bytes have already been through the pipeline N times" *before* the
user commits and six Grok calls are spent. `RunStore.document_history` is the
read-only half of `register_document`, deliberately: registering at probe time
would file a `documents` row whose `first_seen_path` names an upload the user is
about to skip, corrupting the one table whose entire job is identity.

Still proposed: the *automatic* short-circuit — returning the recorded outcome
instead of re-running. Detection is now free; deciding that a re-run should
silently answer from cache is a policy question, and today the human makes it.

### 18. An upload inbox, so intake is not CLI-only — shipped
The dashboard accepts files ([ui/app.py](../ui/app.py)) and a single background
worker drains the queue through the same graph the CLI runs, one at a time,
reporting the live node — `ingest → validate → decide → critique → pay` — onto
the row as it goes. Three details worth defending:

* **Serial is a correctness requirement, not a simplification.** Payment
  idempotency is a read of `invoice_registry` in the `pay` node and a write of
  it in `record` — two transactions. Two runs of one invoice number in flight
  together could both conclude nothing had been paid.
* **The pipeline is built on first dequeue, never at import.** A dashboard with
  no `XAI_API_KEY` still renders, still accepts uploads, and reports the missing
  key against the file that needed it rather than as a blank page.
* **The stage reporter is a LangChain callback**, reading LangGraph's own
  `langgraph_node` metadata, so it cannot drift from the topology the way a
  hand-written string inside each node would. Same seam `recording.py` uses.

---

## Learning from human review

### 19. Precedent-weighted approval — shipped
`human_reviews` was write-only for its whole life. The dashboard filled it in,
the analytics counted it, and nothing ever read it back — so invoice five asked
the question invoice one had asked, got the same escalation, and cost a person
the same five minutes. A queue that never learns is a queue that never shrinks.

The distinction that makes this tractable is *which* questions history can
answer. Some are about this invoice and always will be. Some are about a
vendor's habits — whether they really do bill in EUR, whether their totals drift
by pennies because they round each line rather than the invoice — and a habit is
exactly the sort of thing a handful of consistent human answers settles. Six
findings are on the allowlist in [rules.py](../src/invoiceflow/rules.py):
`unexpected_currency`, the three arithmetic mismatches, and the two due-date
findings. `PRECEDENT_NEVER` states the argument for each exclusion beside it,
and a test asserts every code is on one list or the other.

**`unknown_item` is deliberately not releasable.** It is the tempting one — the
new-SKU case reads exactly like a habit — and it is wrong. Inventory is the
authoritative record of what the company stocks, so an item absent from it is a
question about the catalog, and the fix is to add the SKU rather than to teach
the Approver to stop noticing.

**How much history a finding needs is derived, not configured.** There is no
"three approvals" setting, because three approvals mean very different things
either side of a two-cent drift and a four-hundred-dollar one. Two quantities,
each explainable term by term
([precedent.py](../src/invoiceflow/precedent.py)):

* **burden** — what has to be proven, scaled by what is actually at risk, which
  is *not always the invoice total*. For an arithmetic finding the money at risk
  is the gap, so a two-cent drift is near the floor and a $412 one is at the
  ceiling. Plus half a point for every other warning still open, and a full point
  when the company has never paid this vendor anything — a brand-new vendor with
  a brand-new quirk is the fraud shape, not the supplier shape.
* **support** — one point per prior invoice where a person answered this same
  question, times a comparability factor measured on that same at-risk quantity,
  times a recency factor. Released when `support >= burden`.

It is deliberately not a probability. Nothing here is calibrated against
outcomes, and printing "87% confident" over an ordinal evidence budget would be
a claim the system cannot support.

**Precedent releases the block; it never casts the vote.** A discharged finding
moves out of `advisory_warnings` into `precedent_discharged` carrying the
invoices it was settled on, which is simultaneously what clears the hard rule and
what stops the Critic re-litigating it. The Approver still has to reach approval
as its own affirmative finding, and every ceiling is checked before the
arithmetic is: a critical finding, a code off the allowlist, a single human
rejection on the key, a forged prompt fence, or a total above the scrutiny
threshold ends it regardless of how much support accumulated.

**The load-bearing guard is that automatic approvals never become precedent**,
and it is expressed as the shape of `v_review_precedent` rather than as a filter:
a run reaches that view only by joining `human_reviews`, so one the pipeline
decided by itself has nothing to join to. Without it, one approval votes for the
next decision and that one for the next, and the system ends up citing itself as
the reason it paid.

**The tool is gated on relevance, not on the model's mood.** The Approver's
`find_similar_invoices` — its first tool, over the same loop the Validator uses —
is bound only when the invoice raises a releasable finding *and* history has
cases for it. Everything else takes the path it took before this existed: one
structured-output call, no bound schema, no extra round-trip, and an unextended
system prompt. Asking a model to report that it is unsure would cost the
round-trip the gate was meant to save, since it cannot know until it has looked.
There is no safety net forcing the block in, unlike the Validator's, because a
skipped lookup leaves the pipeline blind about nothing — the rule engine has
already read the same history. Whether the Approver chose to open the file is
recorded either way.

**Day 2.** The weights are hand-set, and hand-set numbers are a placeholder for a
model. The right time to fit one is when there is data, and there is not: the
labels are human review decisions, which is precisely the scarce quantity —
single digits per `(code, subject, vendor)` key for years. And cosine similarity
over invoice text is likely the wrong target anyway, since it conflates documents
that *look* alike with invoices that ask the *same question*; the exact key is
deliberately sharp about the thing worth being sharp about. What would pay is a
supervised model over decision features — amount ratio, days elapsed, vendor
tenure, co-occurring findings, note text — predicting what the human did, which
turns `support >= burden` into a calibrated probability and lets the release bar
be stated in money. `precedent_citations.terms` stores the breakdown rather than
the two totals precisely so that dataset accumulates from today. Two traps come
with it: **censored labels** (once release is live, labels arrive only for what
was escalated, so the training set skews toward the model's own errors — the fix
is routing a random slice of would-be-auto-approvals to a person anyway, which
costs real reviewer time and is a business decision), and **self-confirmation**,
which is the guard above and has to survive any rewrite of the scoring.

The whole thing is demonstrable in the dashboard's **🎓 Learning** tab, over two
vendor histories with deliberately different shapes:
[data/demo/precedent/README.md](../data/demo/precedent/README.md).

---

## Threshold integrity

### 20. Structuring — the $10K rule applied to the money, not to the page — shipped
The brief's approval rule is "invoices over $10K require additional scrutiny",
and the pipeline implemented it the way it is written: a comparison against
`invoice.total`. That is a rule about a *document*, and a rule about a document
is avoided by sending two of them. Splitting one payment into several under-limit
invoices is old enough that banks have a word for it, and it costs a vendor
nothing — three PDFs instead of one.

The fix is not a new kind of finding. It is asking the same question of the right
quantity: before the threshold is applied, [structuring.py](../src/invoiceflow/structuring.py)
reads the registry for every other invoice the same vendor dated within a
fortnight, and `evaluate_rules` compares the threshold against the sum. Three
invoices of $4,860, $4,320 and $5,400 four days apart raise exactly the
`requires_scrutiny` that one $14,580 invoice raises, in the same field, on the
same terms, reaching the Approver and the Critic in the same block. Identical
money, identical treatment — which is the entire claim, and the reason this is
twenty lines of rule engine rather than a new taxonomy of fraud codes.

**It is a scrutiny flag and deliberately nothing harder.** Three invoices in one
week may be three deliveries; the pipeline can establish the pattern and cannot
establish the intent. It does not reject, and it does not force a human the way
`REVIEW_CODES` does — those are the findings where something could not be
*established*, and here the arithmetic is established perfectly well. What the
split may not do is buy a quieter path than the same money on one page. The
scrutiny reason names the sibling invoices, their dates and their standing, so
the next question a reviewer has — "which ones?" — is already answered.

**What counts, and what conspicuously does not.** A `rejected` sibling is left
out: no money moves on a rejection, and one must never push the next honest
invoice over a threshold. An invoice still in the escalation queue is counted,
because it is money queued to leave and the question is what is heading out of
the door this fortnight. The invoice being decided is excluded by number, or a
re-run or a revision would clear the threshold on its own back. Vendor identity
goes through `vendor_key`, the same normalisation precedent uses, because a
company spelled two ways must not be two payment histories — and a rule that
gates automatic payment must not have a second implementation in SQL.

**The window is configuration, unlike the precedent weights.** `structuring_window_days`
describes a company's buying rhythm — a business that orders monthly and one that
orders daily want different numbers — and that is a deployment fact, not an
argument about what evidence is worth. It is symmetric in time, so an invoice
dated back into a fortnight already paid is caught as readily as one arriving
after it.

**Day 2.** Three obvious extensions, none of them free. *Retroactive notice*: the
first two invoices are paid before the pattern exists, and nothing today tells a
reviewer that money already out of the door now looks like part of a split —
worth a dashboard row, and it needs a rule for when to stop looking backwards.
*Grouping on something better than the vendor name*: a purchase-order number or
bank details would catch a split across two vendor spellings the normaliser does
not join, and would also catch the reverse — a genuine coincidence of two
unrelated orders in one week. *A rate, not a sum*: for a vendor who legitimately
bills weekly, the signal is not the fortnight's total but a departure from their
own baseline, which is a model over billing history and wants the data this store
is already accumulating.

---

## Deliberate scope cuts

Named in the README, repeated here because "what we did not build, and why" is
part of the same story: stock is validated but not decremented (reservation
semantics belong to the real inventory system); vendor master-data checks are out
of scope, with the `unknown vendor` fraud signal as the hook they would attach to;
email ingestion is simulated by an email-style fixture, with an IMAP poller
slotting in front of the loader unchanged.
