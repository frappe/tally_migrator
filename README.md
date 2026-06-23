# Tally Migrator

**Move your books from Tally into ERPNext, safely and without re-keying anything.**

Tally Migrator is a Frappe/ERPNext app. You export your data from Tally as a file,
upload it through a guided wizard, and the app creates the matching records in
ERPNext for you: your customers, suppliers, items, accounts, and their opening
balances. It checks everything before it writes, shows you exactly what happened,
and you can undo a whole migration if you need to.

It works from an **uploaded Tally export file**, not a live connection, so it works
even when your ERPNext is hosted in the cloud and your Tally is on an office PC.

> **Full documentation:** the [complete user guide](docs/tally-migrator-documentation.md)
> walks through every step, explains every screen, card, term, warning and error,
> covers the edge cases, and includes a troubleshooting section and glossary.

> _Screenshot: the migration wizard._ <!-- add image: ![Wizard](docs/images/wizard.png) -->

---

## What it brings over

| From Tally | Into ERPNext |
|---|---|
| Customers and suppliers | Customers / Suppliers, with **all** their addresses and phone numbers, bank details, and GST registration |
| Customer / supplier groups | Customer Groups / Supplier Groups |
| Stock items | Items, with their units, HSN codes, and GST tax rates |
| Units of measure | UOMs, including the GST unit codes (UQC) needed for returns |
| Price levels (Retail, Wholesale, MRP) | Price Lists, Item Prices, and discount Pricing Rules |
| Bills of Materials | BOMs (including multiple BOMs per item) |
| Batch-tracked stock | Batches, with manufacturing and expiry dates |
| Chart of accounts and cost centres | Accounts and Cost Centers |
| Warehouses / godowns | Warehouses (including parent/child groups) |
| Opening balances | Opening journal entry, party opening invoices, and opening stock |
| Foreign-currency balances | True multi-currency openings (e.g. a customer who owes you $8,500 comes in as $8,500 at Tally's exchange rate, not converted to a frozen rupee figure) |

After a run, the app produces a **reconciliation report** that puts Tally's opening
trial balance next to what landed in ERPNext, so you can confirm the books match.

---

## Before you start

You need:

- A **Frappe bench with ERPNext installed**. Developed and tested on **version 16**.
- The **India Compliance** app, *if* you want GST details brought over (HSN codes,
  tax rates, GST unit codes, GST registration type). It is optional: without it the
  core records still import, and anything GST-specific is skipped with a note in the
  log so nothing disappears silently.
- Your data exported from Tally as a **Masters XML** file (see below).

### Exporting your data from Tally

In Tally, go to **Gateway of Tally → Export → Masters → Configure**, set the
following, then choose **Export**:

| Setting | Value |
|---|---|
| Type of master | **All Masters** |
| Include dependent masters | **Yes** |
| Export closing balance as opening balance | **Yes** |
| File Format | **XML (Data Interchange)** |

"Export closing balance as opening balance" is what turns last year's closing
figures into this year's opening balances in ERPNext, so don't skip it. Tally
writes a `Master.xml` file (to the folder shown in the export screen) - that's the
file you upload in step 1.

---

## Installation

From inside your bench:

```bash
# 1. Download the app into your bench
bench get-app https://github.com/frappe/tally_migrator

# 2. Install it on your site (the site must already have ERPNext)
bench --site your-site-name install-app tally_migrator
```

Installing creates a **Tally Migration Manager** role. Give this role (or System
Manager) to anyone who should be allowed to run a migration.

---

## How to use it

Open the **Tally Migrator** page from the ERPNext desk (search "Tally Migrator" in
the awesomebar). The wizard walks you through a few guided steps:

> _Screenshot: the wizard steps._ <!-- add image: ![Steps](docs/images/steps.png) -->

1. **Upload** - drop in your Tally export file. The app reads it and tells you what
   it found.
2. **Configure** - pick the ERPNext company the data should go into, choose how to
   handle the chart of accounts, and set the opening-balance date.
3. **Check** - the app validates everything *before* writing: missing states,
   invalid GSTINs, broken account links, and so on. You can fix many issues right
   here, in place, without touching Tally.
4. **Preview** - see a summary of exactly what will be created. (This step appears
   when your file includes a chart of accounts or opening balances.)
5. **Migrate** - the app imports the records and opens a **Migration Log**.

The Migration Log is your record of the run. It shows what was created (grouped by
type, with clickable links to each record), what was skipped and why, and the
reconciliation report. If a large migration is still running, it keeps going in the
background and the log updates as it progresses.

### If something goes wrong

- **You can run it again.** The migration is safe to repeat: records that already
  exist are skipped, never duplicated or overwritten. So if a run is interrupted,
  just run it again and it picks up where it left off.
- **You can undo it.** Each Migration Log has a **Revert** action that deletes the
  records that run created (after you confirm). Reverts run in the background so
  even a large rollback doesn't tie up your screen.

---

## Good to know

- **Your existing records are safe.** Anything that already exists in ERPNext is
  skipped, never duplicated or overwritten, and nothing is deleted (except by the
  Revert action above). The app does make a few small changes that it always notes
  in the log: it sets the currency on a foreign-currency customer or supplier, and
  it may switch on the GST or stock settings needed to import GST details and
  batch-tracked items.
- **Dropped data is always reported.** If the app can't bring something across, it
  says so in the log rather than skipping it quietly.
- **Your Tally file is never modified.** Any fixes you make in the Check step are
  applied to the imported data only; the original export is left untouched.

---

## For developers

This is a standard Frappe app. The source side (`tally/`) reads and maps the XML;
the target side (`erpnext/importers/`) creates the ERPNext documents; `migration/`
orchestrates a run and writes the log.

Tests come in two tiers:

```bash
# Pure tests - no Frappe needed, run anywhere
python3 -m unittest discover -s tally_migrator/tests -p 'test_*.py'

# Full suite - runs on a real site (covers the importers and opening balances)
bench --site your-site-name run-tests --app tally_migrator
```

A few conventions worth knowing before you change core behaviour: keep every run
**idempotent** (safe to re-run), **surface dropped data as a warning** rather than a
silent skip, and add a **new importer subclass** for a new entity instead of editing
existing ones.

---

## License

GNU General Public License v3.0 (GPLv3). Copyright (c) Frappe Technologies Pvt. Ltd. and contributors. See [LICENSE](LICENSE).
