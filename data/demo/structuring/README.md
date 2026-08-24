# Structuring demo — one payment, three invoices

Authored for this project, like `data/demo/precedent/`, and unlike
`data/invoices/`, which is provided case material.

The business rule the brief sets is a threshold on a **payment**: over $10,000
and a person takes a second look. Applied to a *document* it is avoided by
sending three documents. These three are that move — one vendor, one week, three
sums that are individually unremarkable and together are not.

Run them in the order they are dated; the third is the one worth watching:

```bash
uv run python main.py --invoice_path=data/demo/structuring/invoice_6001.txt
uv run python main.py --invoice_path=data/demo/structuring/invoice_6002.csv
uv run python main.py --invoice_path=data/demo/structuring/invoice_6003.json
```

| file | invoice | date | total | vendor's window | outcome |
|---|---|---|---|---|---|
| `invoice_6001.txt` | INV-6001 | 2026-05-04 | $4,860.00 | $4,860.00 | ✅ paid, nothing to notice yet |
| `invoice_6002.csv` | INV-6002 | 2026-05-06 | $4,320.00 | $9,180.00 | ✅ paid — still under the threshold, together |
| `invoice_6003.json` | INV-6003 | 2026-05-08 | $5,400.00 | **$14,580.00** | ⚠️ **scrutinised as one $14,580 payment** |

Every invoice here is legitimate on its face: catalog items within stock,
arithmetic exact to the cent, dates that parse, USD, Net 30. That is the point —
nothing else about them stops the pipeline, so the only thing standing between
the third and a quiet payment is the pattern across all three. Three formats,
because the pattern is in the money and the dates, never in the shape of the
document.

## What actually happens on the third one

`structuring.py` asks the run store what else this vendor billed within
`structuring_window_days` (14) of 2026-05-08, and finds the two already through.
`evaluate_rules` then applies the $10,000 threshold to the sum rather than to the
page, and the Approver is handed a scrutiny reason that names the others:

> total $5,400.00 is under the $10,000 review threshold, but Contoso Supply Co.
> has billed $14,580.00 across 3 invoices dated within 14 days of each other —
> over the threshold together, under it one at a time. Scrutinise this as the one
> payment it adds up to. The others: INV-6001 ($4,860.00, 2026-05-04, paid);
> INV-6002 ($4,320.00, 2026-05-06, paid)

The run's trace carries a `vendor_window` line, and the reason is persisted as a
`scrutiny` row in `rule_reasons`, so a reviewer opening the run later sees the
same three invoices the rule saw.

## What it deliberately is *not*

**Not a rejection, and not a forced human review.** Three invoices in one week
may be three deliveries. The pipeline can establish the *pattern*; it cannot
establish the *intent*, and it does not accuse a vendor of one. What it can do is
refuse to let the split buy a quieter path than the money would have got on a
single page — so the finding is exactly the `requires_scrutiny` that a single
$14,580 invoice raises, and it reaches the Approver and the Critic on the same
terms. Identical money, identical treatment, which is the whole claim.

**Not a verdict on the first two.** They were paid because at $4,860 and $9,180
there was nothing over the threshold to see, and a pipeline cannot scrutinise an
invoice that has not arrived yet. The window is symmetric in time, so a fourth
invoice dated back into the same fortnight would trip it too.

**Not counted where no money is moving.** A `rejected` sibling is left out of the
window — a rejection must never push the next honest invoice over a threshold —
while one still sitting in the escalation queue *is* counted, because it is money
queued to leave. And an invoice never finds itself: a re-run or a revision of
INV-6003 is excluded by number, or it would clear the threshold on its own back.

The same sequence runs offline in `tests/test_structuring.py`, which asserts the
window, the reason, and that no hard rule fires on any of the three.
