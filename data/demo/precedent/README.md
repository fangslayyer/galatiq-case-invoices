# Learning demo — two vendor histories

Authored for this project, unlike `data/invoices/`, which is provided case
material. These exist to make one thing visible: the pipeline escalating an
invoice, a person answering it, and the pipeline eventually stopping asking.

Drive them from the dashboard's **🎓 Learning** tab, which runs them one at a
time and will not let you skip the human step — that step is the demo. The same
sequence runs offline in `tests/test_precedent.py`.

Every invoice here is legitimate. None is clean.

## Track A — Fabrikam GmbH bills in EUR

`unexpected_currency` is a hard `must_review` today: the company settles in USD,
and nothing in the document or our records establishes a rate. Everything else
about these five is immaculate — catalog items within stock, arithmetic exact,
dates parse — so the currency is the only thing standing between each and
approval.

| file | invoice | total | burden | support | outcome |
|---|---|---|---|---|---|
| `invoice_3001.txt` | INV-3001 | €2,400 | 3.48 | 0 | needs review — *you approve* |
| `invoice_3002.json` | INV-3002 | €3,200 | 2.64 | 1.00 | needs review — *you approve* |
| `invoice_3003.csv` | INV-3003 | €4,000 | 2.80 | 2.00 | needs review — *you approve* |
| `invoice_3004.xml` | INV-3004 | €4,200 | 2.84 | **3.00** | **paid, with nobody asked** |
| `invoice_3005.txt` | INV-3005 | €10,750 | 4.00 | 1.80 | needs review — **above the scrutiny threshold** |

INV-3001 carries an extra point of burden that none of the others do: at that
moment the company had never paid Fabrikam anything. A brand-new vendor with a
brand-new quirk is the fraud shape, not the supplier shape.

INV-3005 is the half of the demo worth watching. Same vendor, same currency,
same question, four times the money — and the system declines to apply what it
learned, because $10,000 is the number the business already set as "a person
looks at this" and no weight of precedent reaches past it. Its support also
drops to 1.80: €10,750 is more than twice any sum a person has actually signed
off here, so those three approvals count at reduced weight even before the bar.

Four formats across the five files, because the precedent key is the finding and
the vendor — never the shape of the document.

## Track B — Northwind Traders round each line

Their billing system rounds per line, so the printed grand total sits two cents
off subtotal + tax. `total_mismatch` is advisory rather than a hard rule, but the
Approver escalates on any warning it cannot discharge with evidence — so these
stop too, until there is evidence.

| file | invoice | drift | burden | support | outcome |
|---|---|---|---|---|---|
| `invoice_4001.txt` | INV-4001 | $0.02 | 1.50 | 0 | needs review — *you approve* |
| `invoice_4002.csv` | INV-4002 | $0.03 | 0.50 | **1.00** | **paid, after one approval** |
| `invoice_4003.json` | INV-4003 | **$412.00** | 2.50 | 0.60 | needs review |

This is the "almost would have auto-approved" case. Burden is priced on the
*gap*, not the invoice, so two cents puts essentially nothing at risk and one
human answer carries it. No count was configured to make that happen.

INV-4003 is the same vendor and the same finding with a $412 gap, and it shows
two guards at once. Its support is 0.60, not 2.00, because:

* **INV-4002 does not count at all.** It was approved by the machine, and an
  automatic decision is not evidence about anything — otherwise one approval
  votes for the next, and that one for the next.
* **INV-4001 counts at 0.60, not 1.00.** Approving a two-cent drift does not
  establish approving a four-hundred-dollar one, so the case is real but not
  comparable.

A known penny-rounding habit does not license a $412 discrepancy. That is the
whole design in one row.
