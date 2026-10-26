# Progress checklist

- [x] Phase 1 - architecture and data-model plan
- [x] Phase 2 - skeleton, settings, PostgreSQL, secure admin creation (20 tests passed on the clinic PC)
- [x] Phase 3 - login, lockout, roles/permissions, staff management (68 tests passed on the clinic PC)
- [x] Phase 4 - patients, duplicates/merge, search, appointments, tokens, live queue (154 tests passed on the clinic PC)
- [x] Phase 5 - doctor consultation, vitals, allergies, history, prescriptions (versioned), follow-ups, referrals, minimal medicine catalogue (224 tests passed on the clinic PC)
- [x] Phase 6 - suppliers, purchases, batches, expiry, FEFO, stock ledger, stock reports (291 tests: all code passed; 2 faulty TESTS fixed in patch 6.1; check_stock agrees)
- [~] Phase 7A-1 - roles split (clinic pharmacist / pharmacy manager / counter salesperson), item locations, shared sales engine, shifts, independent counter billing, receipts, discounts, prescription-only approval (files written; waiting for tests on the clinic PC)
- [ ] Phase 7A-2 - returns/refunds/credit notes with approval, counter + consolidated reports, CSV import of locations
- [ ] Phase 7B - clinic-linked dispensing from prescriptions (partial fills), consultation billing, day closing
- [ ] Phase 8 - dashboards, reports, printing
- [ ] Phase 9 - LAN, live updates, services, backups
- [ ] Phase 10 - full test run, end-to-end, manuals

## Known issues / deviations
- One `config/settings.py` driven by `.env` (instead of base/dev/prod files) - simpler to maintain for one clinic.
- Dependency versions are pinned from memory; the sandbox had no internet to verify them. If `pip` says a version does not exist, report it.
- Migrations are generated on your PC with `makemigrations` (Django is not installed in the build sandbox).
- Home page cards marked 'Not built yet' are honest placeholders for later phases.
- Role permissions live in apps/accounts/permissions.py. They need review by the doctor/owner before go-live.
- Lockout (5 wrong passwords -> 15 min) can be abused to lock someone out on purpose; it expires by itself and an administrator can unlock.

- Live refresh uses a small local script (static/js/live.js) polling every 4 s, instead of HTMX: nothing to download, works offline. Background polls do not extend the idle session timeout.
- Patient merge moves every record linked to the duplicate automatically (generic). Phases 5+ must keep finalized-record rules in mind when adding models that point to Patient.
- Doctor can mark a token 'completed' from the queue for now; Phase 5 will tie completion to finishing the consultation.

- Phase 5 adds a minimal medicine catalogue (brand, generic, strength, form, category). It ships EMPTY: no drug data was invented. Phase 6 adds batches, prices, suppliers and stock around it.
- Allergy alerts only match the allergy names recorded for the patient against medicine names. No drug-interaction or cross-reactivity checking exists, because no licensed reference dataset is loaded.
- Append-only (database trigger): audit log, clinical notes/addenda, consultation revisions. Issued prescriptions are locked in the app (changes = new version); a database-level lock for them is a possible later hardening.
- Before real use set: Doctor profile (name, qualifications, registration number), optional Setting `diagnosis_code_system`, optional Setting `require_doctor_registration_on_prescription` (default true).

## Phase 6 notes
- Stock is counted in whole units (tablets, mL...). Purchases are entered in packs; units = packs x units-per-pack. Prices are kept per unit with 4 decimals; bills (Phase 7) round to 2.
- Every stock movement is a row in an append-only ledger (database trigger). Each batch's quantity must equal the sum of its ledger: run `manage.py check_stock` after any restore and weekly.
- FEFO: earliest expiry first, then earliest received. Expired and recalled batches are never dispensed. Optional settings: `near_expiry_days` (default 90), `dispense_min_days_before_expiry` (default 0).
- `stock.consume_stock()` is built and concurrency-tested now; Phase 7 dispensing screens call it. Partial fills use allow_partial=True.
- Purchase prices, valuation and purchase reports need `pharmacy.view_costs` (admin, pharmacy). The doctor sees availability only.
- Not built (deliberately): free/bonus packs, supplier payment tracking, purchase returns as credit notes, PDF exports of stock reports (Phase 8), barcode scanning. Tax on purchases is typed from the invoice, not calculated.

- Patch 6.1: yellow 'draft prescription open' banner with a jump button; '1-1-1-1' frequency suggestion; two test corrections.
- Requirement added: independent pharmacy counter sales (shared stock, separate channel). Planned as Phase 7A/7B; see the integration plan.

## Patch 6.2 / Phase 7A-1 notes
- Doctor screen: live BMI with category (WHO cut-offs by default; `bmi_standard` setting for Asian cut-offs; no adult category for under-18s), quantity auto-calculated from dose x frequency x duration (suggestion; doctor can overwrite), after-food / before-food option on each medicine. The JavaScript is checked against the same test cases as the Python code.
- Roles: `pharmacy` is now the clinic pharmacist only; `pharmacy_manager` holds catalogue, purchases, stock, costs; `counter` sells only. Existing 'pharmacy' users must be reassigned (see docs/COUNTER_MODULE.md).
- A counter sale can never carry a patient or prescription (database constraint). Payments are append-only (database trigger).
- Prescription-only sales need the prescriber name + registration no., the patient's name and a pharmacist's username and password at the counter. Schedule X is blocked. These are software controls: confirm the legal requirements with the Tamil Nadu Drugs Control Department.
