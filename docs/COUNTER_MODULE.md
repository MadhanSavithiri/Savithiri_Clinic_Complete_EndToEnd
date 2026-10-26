# Independent pharmacy counter: setup notes

## Who can do what
| Role | Counter billing | Prescription-only approval | Costs / purchases / stock changes | Locations & prices |
|---|---|---|---|---|
| Independent Pharmacy Counter Salesperson | Yes (sell, take payment, own shift, own sales) | No | **Never** | No |
| Pharmacy Operator (clinic pharmacist) | No | **Yes** | No | No |
| Pharmacy Manager | Yes (can cover the counter) | Yes | Yes | Yes |
| Clinic Administrator | View all counter sales | No | Costs and reports (view) | No |

After installing: open **Staff accounts** and set the right role for each person. The old "Pharmacy Operator"
login keeps only the clinic pharmacist's rights. Give the person who buys stock and edits the catalogue the
**Pharmacy Manager** role, and create a login for the counter salesperson.

## Settings you can change (Django admin -> Settings; key / value)
| Key | Meaning | Default |
|---|---|---|
| `counter_discount_limit_percent` | Largest discount the counter salesperson may give | 5 |
| `manager_discount_limit_percent` | Largest discount the pharmacy manager may give | 10 |
| `enabled_payment_methods` | e.g. `["CASH","UPI"]` | all four |
| `receipt_footer_text` | Line printed at the bottom of every counter receipt | Prices are MRP, inclusive of applicable taxes. |
| `counter_allow_schedule_x` | `true` lets Schedule X be sold at the counter (still needs approval) | false |
| `near_expiry_days`, `dispense_min_days_before_expiry` | Expiry warnings and the earliest-sellable rule | 90, 0 |
| `bmi_standard` | `WHO` or `ASIAN` BMI cut-offs on the doctor's screen | WHO |

## Rack, shelf and box: a simple naming scheme
* **Rack** `R01, R02...` numbered along the wall. **Shelf** `S01...` from the top. **Box** `B01...` left to right.
  Always two digits, so codes sort properly. Keep codes short: they are searched and printed.
* One medicine, one place. Put every batch of that medicine in the same box. FEFO then only needs
  "older batch in front".
* Group by use (fever/pain, antibiotics, stomach, skin, diabetes...) so staff learn the layout quickly.
* Keep prescription-only medicines (Schedule H / H1 / X) together in one lockable rack and mark them in the catalogue.
  Ask the Tamil Nadu Drugs Control Department how they must be stored and recorded.
* Stick the code on the shelf edge and label each box with the medicine name.
* Download the current list (Medicines -> "Download locations (CSV)"), fill the rack/shelf/box columns in Excel,
  and keep it as your master sheet. A CSV import screen is coming in the next update.

## What is NOT built yet (next update, 7A-2)
Returns, refunds and credit notes with approval; counter and consolidated reports; CSV import of locations.
Until then, a mistaken sale is fixed by the pharmacy manager with a stock adjustment, not by editing the bill.
