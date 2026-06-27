# Tally Migrator

Move your books from Tally into ERPNext, safely and without re-keying anything.

Tally Migrator is a Frappe/ERPNext app. You export your data from Tally as a file,
upload it through a guided wizard, and the app creates the matching records in
ERPNext for you: your accounts, customers, suppliers, items, units, price lists,
bills of materials, batches, warehouses, and all of their opening balances. It
checks everything before it writes, shows you a full preview, records exactly what
happened, and lets you undo a whole migration if you need to.

It works from an uploaded Tally export file, not a live connection, so it works
even when your ERPNext is hosted in the cloud and your Tally is on an office PC.
Your Tally file is read only. It is never modified.

> _Screenshot placeholder: the Tally Migrator wizard, step 1._

---

## Table of contents

1. [Who this is for](#who-this-is-for)
2. [What gets imported](#what-gets-imported)
3. [What does not get imported](#what-does-not-get-imported)
4. [Before you begin](#before-you-begin)
5. [Exporting your data from Tally](#exporting-your-data-from-tally)
6. [The wizard, step by step](#the-wizard-step-by-step)
   - [Step 1 - Upload](#step-1--upload)
   - [Step 2 - Configure](#step-2--configure)
   - [Step 3 - Check](#step-3--check)
   - [Step 4 - Preview](#step-4--preview)
   - [Step 5 - Migrate](#step-5--migrate)
7. [The Migration Log](#the-migration-log)
8. [Undoing a migration](#undoing-a-migration)
9. [Re-running a migration](#re-running-a-migration)
10. [Key ideas explained](#key-ideas-explained)
11. [How specific things are handled (edge cases)](#how-specific-things-are-handled-edge-cases)
12. [Reference: every check, warning, and error](#reference-every-check-warning-and-error)
13. [Settings the migration may switch on](#settings-the-migration-may-switch-on)
14. [Troubleshooting and FAQ](#troubleshooting-and-faq)
15. [Glossary](#glossary)

---

## Who this is for

Two kinds of people use Tally Migrator:

- **Accountants and business owners** moving their own books from Tally to ERPNext.
  You do not need to know anything technical. The wizard walks you through every
  step and explains what it found.
- **Implementers and partners** setting ERPNext up for a client. The same wizard,
  plus the Migration Log, gives you a full audit trail of what was created, what
  was skipped, and how the opening trial balance reconciles.

The whole tool is built around one promise: **nothing in your existing ERPNext is
ever overwritten or deleted, and nothing is dropped silently.** If the app cannot
bring something across, it tells you, in the screen and in the log.

---

## What gets imported

This is the full list of what Tally Migrator brings over, and the ERPNext records
it creates for each.

| From Tally | Into ERPNext | Notes |
|---|---|---|
| Customers and suppliers | Customer / Supplier | With all their addresses, all their phone and email contacts, bank details, GST registration, PAN, credit limit, and credit period |
| Customer / supplier groups | Customer Group / Supplier Group | Recreated from each party's Tally parent group |
| Stock items | Item | With unit, HSN/SAC code, GST tax rate and treatment, valuation method, stock vs service |
| Item / stock groups | Item Group | The full nested tree, not a flat list |
| Units of measure | UOM | With the whole-number flag, compound conversions (for example 1 Dozen = 12 Nos), and the GST unit codes (UQC) needed for returns |
| Price levels (Retail, Wholesale, and so on) | Price List + Item Price | One Price List per level, one Item Price per item on that list |
| Per-level discounts | Pricing Rule | Scoped to that price list, so the discount applies only when you sell at that level |
| Maximum Retail Price (MRP) | A Price List named "MRP" + Item Price | Tally MRP has no native ERPNext field, so it is modelled as its own price list |
| Bills of materials | BOM | Submitted and active. Multiple BOMs per item are supported; the first is the default |
| Batch-tracked stock | Batch | With manufacturing and expiry dates |
| Chart of accounts | Account | Groups and ledgers, classified into ERPNext's asset / liability / equity / income / expense structure |
| Cost centres | Cost Center | Flat or nested |
| Warehouses / godowns | Warehouse | Including parent and child groups, linked to the company stock account |
| Bank ledgers | Bank + Bank Account | The company's own bank accounts, and parties' bank accounts |
| Ledger opening balances | Opening Journal Entry | One balanced, submitted opening entry |
| Customer / supplier opening balances | Opening Sales / Purchase Invoices and Payment Entries | Posted bill by bill, so you can reconcile future payments invoice by invoice |
| Foreign-currency balances | True multi-currency openings | A customer who owes you $8,500 comes in as $8,500 at Tally's recorded exchange rate, against a currency-specific receivable account, not converted to a frozen rupee figure |
| Item opening stock | Opening Stock Reconciliation | With per-godown and per-batch placement where Tally carries it |

After a run, the app produces a **reconciliation report** that puts Tally's opening
trial balance next to what landed in ERPNext, so you can confirm the books match.

---

## What does not get imported

This version migrates **master records and opening balances only**. The following
are deliberately out of scope. When the app sees them in your file, it tells you in
the Migration Log rather than dropping them silently.

- **Transactions and vouchers** (sales, purchases, payments, journals during the
  year). Only the opening balances are brought over, not the year's transaction
  history.
- **TDS and TCS configuration** (tax deducted / collected at source). Your ledger
  balances still import in full; only the tax-deduction setup is skipped. The log
  names this as a deliberate skip.
- **Employee and payroll / HR details.** Tally exports employees as cost centres
  carrying payroll fields (gender, PF number, bank, dates of birth and joining).
  The cost centres themselves import, but these HR fields do not. Bring employees
  across separately through HR / Payroll.
- **Co-products, by-products, and scrap in a BOM.** Only true Component rows are
  imported into the ERPNext BOM. The others are skipped with a warning.
- **Foreign-currency advances.** A foreign-currency receivable or payable invoice
  is migrated; a foreign-currency advance or credit is flagged for you to enter
  manually.
- **Custom fields (UDFs) and any Tally attribute outside the supported mapping.**
  These are listed for you in the log's coverage report so you can decide whether a
  custom field matters to your business.

---

## Before you begin

You need:

- A **Frappe bench with ERPNext installed**. Tally Migrator is developed and tested
  on **ERPNext version 16**.
- A **company already created** in ERPNext, with its standard setup in place
  (default customer / supplier / item groups, a territory, receivable and payable
  accounts, a fiscal year, and the warehouse tree). This is normally created for
  you when you make the company. The wizard checks all of this for you on the Check
  step and tells you if anything is missing.
- The **India Compliance** app, only if you want GST details brought over (HSN
  codes, GST tax rates, GST unit codes, and GST registration type). It is optional.
  Without it, the core records still import and anything GST-specific is skipped
  with a note in the log, so nothing disappears silently.
- Your data exported from Tally as a **Masters XML** file (see the next section).

### Installing the app

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

## Exporting your data from Tally

The single most important step happens in Tally, before you ever open ERPNext. If
you export the wrong scope here, the migration has nothing to work with. Get this
right and the rest is guided.

In Tally Prime:

1. Open your company.
2. Go to **Gateway of Tally > Export > Masters**, then open **Configure**.
3. Set these options:

| Setting | Value | Why it matters |
|---|---|---|
| Type of master | **All Masters** | This is what includes your customers, suppliers, items, and warehouses. If you pick a narrower scope, the file will be missing most of your data |
| Include dependent masters | **Yes** | Brings in the groups, units, and other records your masters depend on |
| Export closing balance as opening balance | **Yes** | This is what turns last year's closing figures into this year's opening balances in ERPNext. Skip it and your opening balances will be empty |
| File Format | **XML (Data Interchange)** | The format Tally Migrator reads |

4. Choose **Export**, and note where the `Master.xml` file is saved. That is the
   file you upload in step 1 of the wizard.

> _Screenshot placeholder: the Tally export configuration screen with these four settings._

### Two Tally settings that change what you get

A couple of Tally features decide how rich your migration can be. They are not part
of the export dialog above; they are how your Tally company was set up.

- **Maintain bill-wise details.** If this was on in Tally, each customer and
  supplier opening balance carries its individual outstanding bills. Tally Migrator
  then creates one opening invoice per bill, so you can reconcile future payments
  invoice by invoice. If it was off, Tally only stores a single combined balance per
  party, and the migration posts one opening invoice for that whole balance. See
  ["No bill detail"](#no-bill-detail) on the Preview step.
- **Maintain batch-wise details.** If this was on for an item, its opening stock is
  split by batch (with manufacturing and expiry dates), and Tally Migrator recreates
  those batches. If it was off, the item's stock imports as a single quantity.

You do not have to change these now. The migration works either way; this just
explains why your preview may show combined balances instead of per-bill ones.

---

## The wizard, step by step

Open the **Tally Migrator** page from the ERPNext desk (type "Tally Migrator" into
the search bar at the top). A stepper across the top shows where you are.

The steps are **Upload, Configure, Check, Preview, Migrate**. The **Preview** step
only appears when your file carries a chart of accounts or opening balances. A
masters-only file (just customers, items, and so on) shows a clean four-step flow.

> _Screenshot placeholder: the wizard stepper showing all five steps._

You can leave at any point and come back. Your progress, and every fix you make
along the way, is saved automatically. When you return, a banner offers to **resume
your unfinished migration** or **start over**. If a migration was already running
when you left (for example you reloaded the page mid-import), resuming reconnects to
that run and tracks its progress to completion - it never starts a second one.

### Step 1 - Upload

Click **Choose Tally XML or .zip file** and pick the `Master.xml` you exported. The
app reads it and shows you what it found, as a row of count chips: Customers,
Suppliers, Items, Warehouses, and (when present) Accounts and Cost Centres.

> _Screenshot placeholder: step 1 after a successful upload, showing the count chips._

**Importing a large file.** The browser upload is capped (commonly around 25 MB),
but a Masters XML compresses by roughly 90%, so a big export has two easy routes:

- **Upload a zip.** Zip the `Master.xml` and choose the `.zip` - the app unpacks it
  automatically and reads the XML inside. The zip must contain exactly one `.xml`
  file and must not be password-protected. (The uncompressed XML is still held to
  the same size limit, so a zip cannot be used to slip an over-limit file past the
  cap.)
- **Import from a Google Drive link.** In the upload dialog, choose the **Link**
  option and paste the share link to your file. In Drive, the file must be shared as
  **Anyone with the link** - otherwise Google returns a sign-in page instead of the
  file and the import is refused with a clear message. The app downloads the file on
  the server, so nothing has to pass through your browser. A zipped XML on Drive
  works the same way. Only Google Drive links are accepted; any other link is
  rejected.

This preview is your first and best check that you exported the right thing. Read
the counts carefully.

**What the messages mean:**

- **"File read successfully. Here's what we found:"** (green) - the file is a valid
  masters export and carries business data. The counts tell you how much.
- **Some counts are zero (for example 0 Customers, 0 Suppliers).** This almost
  always means you exported the wrong scope from Tally - most often you did not pick
  **All Masters**, or you exported a single ledger or group instead of the whole
  company. Go back to Tally, re-export with **Type of master = All Masters**, and
  upload again.
- **"This looks like a chart-of-accounts-only export."** (blue) - the file has
  accounts and opening balances but no customers, suppliers, items, or warehouses.
  That is a valid thing to import if you only want the chart of accounts. But it is
  also a common sign you exported a narrower scope than you meant to. If you
  expected your parties and items too, re-export from Tally with **All Masters** and
  upload again. If a chart-of-accounts-only import is what you want, just continue.
- **"We read the file, but found no Accounts, Customers, Suppliers, Items or
  Warehouses in it."** (red) - the file parsed but is empty of anything we can
  import. Re-export **Masters** from Tally with **Show All Masters = Yes**.
- **"We couldn't read this file."** (red) - the file is not a valid Tally Masters
  XML export, or it is corrupted. Re-export and try again.

When the file looks right, click **Continue**.

### Step 2 - Configure

Three choices tell the migration where the data goes and how to treat your accounts.

> _Screenshot placeholder: step 2 with the company, chart-of-accounts, and date fields._

**ERPNext Company.** Pick the company that will receive the records. If you have
only one company, it is selected for you. If you have none, you will see a link to
create one first; do that, then come back and refresh the page.

**Chart of Accounts.** This decides how your Tally account groups map into ERPNext.

- **Reuse ERPNext's standard accounts (recommended).** Tally's standard, named
  groups (like Sundry Debtors, Current Assets, and so on) are matched onto ERPNext's
  built-in chart of accounts. Only your custom groups and your individual ledgers
  are created. This keeps your ERPNext chart clean and standard.
- **Mirror Tally's group tree exactly.** Every Tally group is recreated in ERPNext,
  verbatim, preserving your exact tree. Choose this if you want your ERPNext chart
  to look exactly like Tally's.

**Opening-balance date.** The posting date for your opening balances and opening
stock. Leave it blank to use the company's current fiscal-year start date, which is
the usual choice. If you set a date that falls inside a frozen accounting period or
outside any fiscal year, the Check step warns you, because the opening entries would
not be able to post on that date.

Below the choices you see **"Here's what will be imported"** with the same count
chips from step 1, so you can confirm before moving on. Click **Continue**.

### Step 3 - Check

This is the pre-flight. The app compares your file against what already exists in
ERPNext and against ERPNext's rules, so you can fix problems **before** anything is
written. Nothing is changed automatically on this step. You can fix many issues
right here, in place, without touching Tally.

> _Screenshot placeholder: step 3 with the data-quality cards and an expanded issue group._

The check has several parts. Any of them can appear; a clean file shows a simple
**"Nothing to resolve"** message and you continue straight away.

#### Data-quality report

Three cards summarise it:

- **Mapped** - the total number of records read from your file (customers,
  suppliers, items).
- **Errors** - the number of distinct issue types that would stop a record from
  importing. These need attention.
- **Warnings** - the number of distinct issue types that will still import but are
  worth a look.

Below the cards, each issue is grouped by type. Click a group to expand it and see
the affected records. Where an issue is fixable, you get an input box (or a state
dropdown) pre-filled with the current value. Type a correction and click
**Re-check** to re-validate. Your edits never touch the Tally file; they ride along
as in-memory fixes and are recorded in the log afterwards.

The issue types you may see (each is explained in full in the
[reference section](#data-quality-checks)):

| Shown as | Severity | Meaning in one line |
|---|---|---|
| Invalid GSTIN | Error | The GST number fails its format or checksum |
| Item code collision | Error | Two Tally items would become the same ERPNext item code |
| Imports without a GST state | Warning | An Indian party has no state set |
| GSTIN and state to verify | Warning | The GSTIN's state code does not match the ledger state |
| PIN and state to verify | Warning | The PIN code looks like a different state |
| Imports without the email | Warning | The email address is malformed |
| Imports without HSN | Warning | An item has no HSN/SAC code |
| Possible duplicate to review | Warning | Two or more parties look like the same real entity |
| Will merge into one | Warning | Two items / warehouses / units share a name and will merge |
| Hierarchy loop simplified | Warning | A parent chain forms a loop |

**Errors require your consent.** If any record has an error, a checkbox appears:
"Some records have errors and won't import. Continue with the rest." You must tick
it to continue. The records with errors are skipped; everything else imports, and
you can fix and re-import the rest later.

#### Company readiness

This checks that the target company is actually set up to receive masters. It
appears only when something needs attention.

- **Blockers** (red) stop the run. A blocker means a whole type of record would
  fail. For example, if the default Customer Group is missing, every customer would
  fail. Each blocker tells you exactly what to create. Fix it in another tab, then
  click **Re-check**.
- **Warnings** (blue) do not block. They mean part of the migration is degraded but
  masters still import. For example, if the company has no default receivable
  account, customer opening balances are skipped, but the customers themselves
  import fine.

The full list of blockers and warnings is in the
[reference section](#company-readiness-checks).

#### Units of measure

If your file uses units that do not yet exist in ERPNext, they are listed here. By
default each one is created as a new unit, so three or three hundred units all
resolve in a single click. If you would rather map a Tally unit to one you already
use (say, map Tally "Pcs" to your existing "Nos"), choose it from the dropdown on
that row. A **"Set all to create as new"** button is there for convenience.

#### Field coverage notice

If your file contains fields that ERPNext has no place for (Tally custom fields, or
attributes we do not map), a notice names them in plain language so you can judge
whether any matter to your business. For a standard Tally export, this stays silent.
The full, itemised list is always saved on the Migration Log regardless.

When everything is resolved (or you have consented to skip the error records), click
**Continue**.

There is also a **Start over** button here, which discards the file and every fix
you have made (after a confirmation), in case you want to begin again with a
different export.

### Step 4 - Preview

This step appears when your file carries a chart of accounts or opening balances. It
shows you exactly how your accounts will be classified and how your opening balances
will post, before you commit. Everything here is read-only.

> _Screenshot placeholder: step 4 showing the accounts summary cards and the party opening cards._

#### Your accounts

Three summary cards:

- **Mapped by standard groups** (green, "high confidence") - the ledgers that map
  cleanly using Tally's standard, named groups. No guessing was needed.
- **We had to infer** (blue, "please check") - ledgers that sit under a *custom*
  Tally group, so we worked out their type (asset, liability, income, expense) from
  the group's own nature. These are the rows worth your eye. A type shown as "--"
  is one we genuinely could not determine; set it in ERPNext after import, or fix
  the group in Tally and re-upload.
- **Opening balances** - shows **Balanced (Dr = Cr)** when your opening balances net
  to zero on their own, or the amount held in **Temporary Opening** when they do not.

When there are inferred accounts, a table lists each one with the type we assigned
and its opening balance, largest balance first. When there are none, you get a green
confirmation that all accounts mapped using Tally's standard groups.

A **"Show all ... mapped accounts"** disclosure expands the full chart of accounts,
grouped by class, with each ledger's account type, parent group, and opening
balance.

**About Temporary Opening.** Tally does not force opening balances to net to zero;
the leftover is its own "Difference in Opening Balances". ERPNext absorbs that
leftover into an account called **Temporary Opening** so your books stay balanced.
A non-zero amount here is normal and expected, not an error. It is the part of your
Tally opening that does not balance on its own, plus any income or expense opening
balances that ERPNext does not allow on an opening entry. You clear it as you finish
your opening entries.

#### Customer and supplier opening balances

This is where bill-wise detail shows up. Cards summarise how party balances will
post:

- **Outstanding invoices** ("one opening invoice each") - each outstanding bill
  becomes one opening Sales or Purchase Invoice, so future payments reconcile
  against it individually.
- **Advance receipts/payments** ("one payment entry each") - advances and credit
  balances become opening Payment Entries, left unallocated and ready to apply
  against a future invoice.
- One of two cards depending on your data:
  - <a name="no-bill-detail"></a>**No bill detail** ("single opening invoice") -
    these parties had no bill-wise breakup in Tally, because **Maintain bill-wise
    details** was off in Tally for them. Each gets one opening invoice for its full
    balance, instead of a separate invoice per outstanding bill. This is expected and
    fine; it just means you reconcile that party as a single outstanding amount.
  - **Bills didn't reconcile** ("posts On Account") - for these parties, the
    individual bills did not add up to the party's ledger opening balance. The
    difference is posted as one "On Account" opening so the party still ties to the
    trial balance. This is worth checking in Tally: a bill may be missing or
    mis-dated. A table lists each such party with its ledger opening and the
    unreconciled gap, largest gap first.

If any parties use a **foreign currency**, a note tells you how many. Their opening
balances post in that currency at the rate recorded in Tally, bill by bill when the
file carries per-bill amounts, otherwise as a single opening invoice.

A **"Show all ... parties"** disclosure lists every party with an opening balance,
its type, how many opening documents it produces, and its opening amount.

Click **Continue** when you have reviewed it.

### Step 5 - Migrate

The final step imports the records.

> _Screenshot placeholder: step 5 showing the results table after a run._

Click **Run Migration**. A progress bar shows the stages (reading the file,
extracting masters, importing each entity type, posting opening balances, saving the
log). It advances continuously within a stage and shows a live count (for example,
"Importing suppliers 8,432 of 13,088"), so a large stage never looks stuck. For a
large file, the import keeps running in the background and you can leave the page; the
result still appears here and in the log.

When it finishes, you see a results table with one row per record type and these
columns:

- **Imported** - newly created in ERPNext.
- **Already there** - skipped because the record already existed. This is safe;
  nothing was changed or duplicated.
- **Warnings** - the record imported, but a dependent piece (an address, a contact,
  an opening balance, and so on) was dropped. See the log for the detail.
- **Failed** - the record could not be imported. See the log for the reason.

The headline above the table is one of three states, so a partial problem is never
hidden behind a green "all done":

- **All done** (green) - everything imported with no warnings or failures.
- A blue summary - records imported, but some warnings need a look.
- A red summary - most records imported, but some failed and need your attention.

A **View migration log** button takes you to the full record of the run. A
**Migrate another file** button resets the wizard for the next company or file.

---

## The Migration Log

Every run creates a **Tally Migration Log**. It is the complete, permanent record of
what happened, and the place you go to review, reconcile, re-run, or undo. Open it
from the results screen, or find it in the desk under **Tally Migration Log**.

> _Screenshot placeholder: a Migration Log with its summary dashboard and reconciliation table._

The log's **status** is one of:

- **Running** - the migration is still in progress. The log does not refresh on its
  own, so reload it to see the latest progress. If the run stops early (for example
  the server restarts or runs out of memory), the log detects that its background job
  is no longer active and shows that the run stopped before completing - records
  imported so far are kept, so you can run it again.
- **Completed** - finished with no failures.
- **Completed with Errors** - finished, but some records failed (everything else
  imported).
- **Failed** - the run hit a fatal error. Records imported before the failure are
  kept, because each step commits as it completes, so it is safe to run again.
- **Reverted** / **Reverted with Errors** - the migration was undone (see
  [Undoing a migration](#undoing-a-migration)).

The log is made up of several sections, each explained below.

### Import summary

A per-record-type table with a coloured bar for each row showing the split of
**Imported (new)** in green, **Already there (skipped, safe)** in grey, and
**Failed** in red, plus **Imported**, **Already there**, **Warnings**, and
**Failed** count columns and a total row. The **Warnings** column counts records
that imported but had a dependent piece (an address, a contact, an opening balance,
and so on) dropped, so a partial drop is visible here and not only on the results
screen; the detail is in the issues table below. This is the same breakdown you saw
on the results screen, kept for the record.

### Reconciliation: opening trial balance

This is the accountant's proof that the books match. It builds the opening trial
balance two ways and puts them side by side:

- **Tally** - what your file said the openings were.
- **ERPNext** - what ERPNext actually holds now, read back from the general ledger
  and stock.

Rows are grouped by account class (Assets, Liabilities and Equity, Income, Expense),
plus separate **Receivables** and **Payables** lines (the Debtors and Creditors
control totals), a **Stock value** line, and the **Temporary Opening** line. A tick
marks each row that matches. The totals row shows Dr equals Cr on both sides.

The verdict at the top is one of:

- **Reconciled** (green) - the opening trial balance matches ERPNext.
- A review note (red) - a figure differs; check the rows.
- **Showing Tally's trial balance only** (blue) - ERPNext's side could not be read
  back, so only Tally's column is shown.

There is one special case. If you import more than one different Tally export into
the **same** company, the ERPNext column becomes the running total of all of them,
so it cannot line up with any single file. When the app detects this, it replaces
the red "a figure differs" alarm with a calm note explaining that this is expected.
For a clean per-file reconciliation, import each Tally export into its own company.

**Why Temporary Opening can be large and that is fine:** see
[the explanation above](#your-accounts). It is Tally's own opening difference, made
explicit, and it makes the trial balance net to zero.

### Records created

The authoritative "what did this run touch" list: every ERPNext document the
migration inserted, grouped by document type, with a clickable link to each one.
This includes the opening Journal Entry, the opening invoices and payment entries,
and the stock reconciliation, so the whole run is reviewable and reversible by
inspection.

### Pre-flight data quality

The data-quality issues that were flagged before the run (the same ones from the
Check step), kept for the record, grouped by type with the affected records.

### Applied edits

If you changed anything on the Check step, this lists each change so you have an
authoritative record of what you adjusted. It covers two kinds of edit:

- **Field fixes** - which field on which record changed, with its old and new values
  (for example a customer's PIN code or GSTIN).
- **Unit resolutions** - each Tally unit you resolved, shown as either "Mapped to
  existing unit" (for example Carton to Box) or "Created as new unit", with the Tally
  name and the resulting ERPNext unit.

The uploaded file itself is never modified, so this table is the authoritative record
of what you changed before import. (Automatic unit normalisations the app does on its
own, such as "Kgs" to "Kg", are not listed here because they are not your edits.)

### Accounts mapping

A durable record of how each Tally ledger was classified into ERPNext accounts,
which ones had to be inferred, and the Temporary Opening residual. This mirrors what
you saw on the Preview step, kept so the classification is reviewable after the run.

### Field coverage

A complete, honest audit of every field in your file:

- Fields **imported** (reached ERPNext).
- Fields **already captured via another field** (a flat tag that duplicates data we
  already read from a nested one; not a loss).
- **Tally-internal** fields set aside (config flags, empty containers, audit stamps,
  pre-GST tax scaffolding; no business value, but listed so nothing is hidden).
- Fields **not migrated** (custom fields / UDFs we never read, and any field read
  but not persisted). For each, you see what it looks like (a Yes/No flag, a
  category, a reference, free text), how often it was filled, and a sample value, so
  you can decide whether it matters.

A reconciliation line confirms that **all** fields in your file are accounted for,
so nothing was dropped without a record. TDS/TCS and employee/HR fields, when
present, are named here as deliberate scope decisions rather than silent losses.

---

## Undoing a migration

Every run that created records can be undone. On the Migration Log, open the
**Actions** menu and choose **Undo This Migration**.

> _Screenshot placeholder: the undo confirmation dialog showing the breakdown of records to delete._

A confirmation dialog shows exactly what will be deleted, broken down by document
type (for example "12 Item, 5 Customer, 3 Journal Entry"). To confirm, you type the
company name. This is a deliberate, destructive action, so the confirmation is
strict.

How the undo behaves:

- Submitted entries (invoices, journal entries, stock reconciliations) are cancelled
  first, then deleted.
- Anything that is now linked to activity created **after** this migration is
  **kept**, not force-deleted, and listed for you. For example, if you raised a real
  invoice against a migrated customer, that customer is kept.
- Only this migration's own records are touched. Nothing else in the company is
  affected.
- The deletion runs in the background, so even a large undo does not tie up your
  screen. You get a link to a **Tally Migration Revert** record where you can watch
  progress and see, document by document, what was deleted and what was kept.

After a clean undo the log becomes **Reverted**. If some records had to be kept, it
becomes **Reverted with Errors** and the undo stays available so you can retry after
clearing the cause.

---

## Re-running a migration

The migration is **safe to run again at any time.** Records that already exist are
skipped, never duplicated or overwritten. So if a run is interrupted, or some
records failed, just run it again and it picks up where it left off, importing only
what is missing.

There are two ways to re-run:

- **From the wizard** - upload the same file and walk through again. Already-imported
  records are skipped.
- **From the Migration Log** - if a run finished with errors or failed, a **Re-run
  from Source File** button appears. It re-runs from the same file, with the same
  options and the same pre-flight fixes you made, and creates a new log. In practice
  only the previously failed records are retried.

Opening balances have their own safeguards so a re-run never doubles your books. The
opening journal entry is posted in batches, and each batch is marked so a re-run
skips exactly the batches already posted and completes any that did not. Opening
invoices, advances, and opening stock are each marked the same way. And if the
company already has an opening entry that **this tool did not create** (for example,
you opened the books by hand), the migration leaves it alone and skips opening
balances entirely rather than risk double-posting.

---

## Key ideas explained

A few concepts come up throughout the tool. Understanding them makes everything else
clearer.

**Idempotent (safe to re-run).** Every importer checks whether a record already
exists and skips it if so. Running the migration twice produces the same result as
running it once. This is the core safety promise.

**Your existing data is safe.** Nothing is ever overwritten or deleted by a
migration. The only changes the app makes to existing records are small and always
noted in the log: it sets the currency on a foreign-currency customer or supplier,
and it may switch on a few GST or stock settings needed to import GST details and
batch-tracked items (see [Settings the migration may switch on](#settings-the-migration-may-switch-on)).

**Dropped data is always reported.** If the app cannot bring something across, it
records a warning in the log. Nothing is skipped silently. This is why the results
have three states (done / warnings / errors) and why warnings are counted
separately.

**Temporary Opening.** The account that absorbs any opening-balance difference so
your books balance. A non-zero balance here is normal: it is Tally's own opening
difference plus any income/expense openings ERPNext cannot carry. Clear it as you
finish your opening entries.

**Opening invoices vs a journal entry.** Ledger account openings (cash, assets,
loans, and so on) post as one balanced opening Journal Entry. Customer and supplier
openings post as opening **invoices** instead, one per outstanding bill, so you keep
bill-level traceability and can reconcile future payments invoice by invoice. This
is richer than a single lump journal line per party.

**True multi-currency openings.** A foreign-currency party's opening posts in its
own currency, against a currency-specific receivable or payable account (for example
"Debtors USD"), at Tally's recorded rate. The outstanding amount tracks the foreign
figure, and the base currency reconciles. It is not flattened to a frozen rupee
number.

**Reserved vs inferred accounts.** A "reserved" account is one mapped by a named,
standard Tally group, so its classification is high-confidence. An "inferred"
account sits under a custom group, so its type was worked out from the group's own
nature. Inferred accounts are the ones the Preview step asks you to confirm.

---

## How specific things are handled (edge cases)

This section answers the "but what about ..." questions. Each item is how Tally
Migrator handles a real-world wrinkle in Tally data.

### Parties (customers and suppliers)

- **Multiple addresses per party.** Tally's address book (beyond the main mailing
  address) is fully imported. Each extra address becomes its own ERPNext Address
  linked to the party. The address type is taken from the Tally label when it
  matches a standard type (Billing, Shipping, Office, and so on), otherwise it is
  typed "Other" with the label preserved in the title.
- **Each address needs a state.** ERPNext (with India Compliance) requires a state on
  an Indian address. For each address, the app uses the most precise signal it has:
  the address's own state, then the state derived from its own PIN code, then the
  party's state. So a party with a valid address rarely loses it for a missing state.
- **Multiple phone numbers and contacts.** Tally's extra named contacts are each
  imported as their own ERPNext Contact, linked to the party. The number Tally marks
  as the WhatsApp default is set as the primary mobile.
- **The primary address and contact are linked.** ERPNext does not automatically mark
  a party's primary address or contact when they are created separately, so the app
  sets them explicitly. Without this, migrated parties would show no primary address
  or contact.
- **Bank details.** A party's bank account (account number and IFSC) is imported as
  an ERPNext Bank Account, creating the Bank master if needed.
- **Credit limit and credit period.** A Tally credit limit becomes the company-scoped
  credit limit on the party. A credit period like "30 Days" maps to a Payment Terms
  Template named "Net 30" when one exists.
- **Invalid GSTIN.** A structurally invalid GSTIN is dropped from the party (and its
  address) so it does not block import, but the party still imports as Unregistered.
  The pre-flight flags it so you can fix it.
- **PIN that does not match the state.** India Compliance rejects an address whose PIN
  does not match its state. Rather than lose the whole address, the app drops just
  the PIN, keeps the address, and warns you to set the PIN in ERPNext.
- **Bad email.** A malformed email would cause ERPNext to reject the party's contact
  record. The pre-flight flags it as a warning so the party still imports with its
  other contact details.
- **Party groups.** A party's Tally parent group (for example "Trade Debtors -
  Domestic") is recreated as a group in ERPNext and assigned. If a group cannot be
  created, the party falls back to the standard default group, with a warning so the
  lost grouping is visible.
- **Duplicate parties.** Two parties that look like the same real entity (by
  normalised name, shared GSTIN, shared phone, or close name match) are **flagged for
  review only**. The migration never merges or drops them - every distinctly named
  party is imported as its own record exactly as it is in Tally. Merging, if you want
  it, is something you do yourself in ERPNext afterwards. (On very large files the
  close-name-match check is skipped for speed - exact matches by name, GSTIN, or phone
  are still flagged at any size.)
- **GST category.** Tally's explicit registration type wins when set (it is the only
  thing that distinguishes Composition or SEZ). Otherwise, a party outside India is
  "Overseas", and an Indian party's category is inferred from its GSTIN.

### Items

- **Item code collisions.** Two different Tally item names can reduce to the same
  ERPNext item code (codes are truncated to 140 characters and "/" becomes "-"). The
  first wins; the second is skipped with a warning naming both, so you can rename one
  in Tally. The pre-flight flags this too.
- **Unit resolution.** An item's Tally base unit maps to an ERPNext UOM: your chosen
  mapping first, then the built-in unit map, then the Tally unit's own name if a UOM
  by that name exists, and only "Nos" as a last resort.
- **Stock item vs service.** A Tally stock item whose GST supply type is "Services"
  becomes a non-stock Item in ERPNext. Everything else stays a stock item.
- **Valuation method.** Tally's costing method maps across: average becomes Moving
  Average, FIFO and LIFO map straight. Anything else keeps the ERPNext default.
- **GST treatment.** Taxable, Nil-Rated, Exempt, and Non-GST are mapped to the
  matching India Compliance flags. An unrecognised value defaults to taxable, with a
  warning. The item's GST tax rate is linked to the matching India Compliance tax
  template ("GST 18%", and so on) for the company.
- **Missing HSN.** An item with no HSN/SAC still imports; the pre-flight warns,
  because GST invoices for it will not be compliant until you add one. If India
  Compliance rejects a specific HSN as invalid, the item is retried with the HSN
  cleared so it still lands, and you are told to set a correct one.
- **Batch and expiry.** An item marked batch-wise in Tally gets batch tracking turned
  on; if it is also perishable, expiry tracking is enabled too.

### Units of measure

- **The unit masters themselves** (formal name, decimal places) are imported, not
  just resolved by name. A unit whose decimal places are zero is marked
  whole-number-only.
- **Compound units** (for example 1 Dozen = 12 Nos) become a UOM Conversion Factor.
- **A UOM Category** is required for conversions in recent ERPNext. If none exists,
  the app creates one called "Tally Imported" and notes it in the log.
- **GST unit codes (UQC).** Each unit's Tally UQC is mapped into India Compliance's
  GST settings (for example "NOS (Numbers)"), which GST returns need. A Tally code
  with no standard GST equivalent is left unmapped with a warning.

### Accounts, cost centres, and warehouses

- **Reserved groups** (standard, named Tally groups) are not recreated in reuse mode;
  their ERPNext equivalents are used as parents. In mirror mode, every group is
  recreated.
- **Account ordering.** Groups are created parent-before-child, and any circular
  parent loop is broken rather than crashing (with a pre-flight warning).
- **Bank ledgers.** A Tally bank ledger carrying the company's own account number and
  IFSC creates a company Bank Account linked to that GL account.
- **Cost centres** import flat or nested, under the company's root cost centre.
- **Warehouses / godowns** import with their full nesting. A godown that is the parent
  of another is created as a group warehouse so the tree renders correctly. Each
  (non-group) warehouse is linked to the company's stock account.
- **Stock groups** recreate Tally's nested item-group tree, so items nest under a real
  hierarchy rather than a flat list.

### Bills of materials

- Each Tally BOM becomes a submitted, active ERPNext BOM. The first BOM per item is
  the default; multiple BOMs per item are supported.
- Only true **Component** rows are imported. Co-products, by-products, and scrap are
  skipped with a warning.
- A component that is not found as an item, or that is the finished item itself
  (a self-reference), or that has no quantity, is skipped with a warning.
- If a component's quantity is in a unit different from its stock unit, the BOM still
  imports but you are warned to verify the quantity, because ERPNext assumes a 1:1
  conversion unless the item defines otherwise.

### Prices

- Each Tally price level becomes a selling Price List, and the level's rate becomes an
  Item Price on that list.
- A per-level discount becomes a Pricing Rule scoped to that price list, so the
  discount applies only when you sell at that level (mirroring Tally's "rate then
  discount" billing).
- Tally MRP becomes an Item Price on a Price List named "MRP".
- If a price's unit is not valid for the item, the item's stock unit is used instead,
  with a warning.

### Opening balances and opening stock

- **Ledger openings** post as one balanced, submitted opening Journal Entry, built in
  batches by account class so a problem in one class does not lose the whole trial
  balance.
- **Income and expense openings** cannot carry an opening balance in ERPNext, so they
  are not posted as account lines; their amount stays inside the Temporary Opening
  difference (and the reconciliation accounts for this), with a warning.
- **Party openings** post bill by bill as opening invoices and payment entries, as
  described on the Preview step.
- **Opening invoices are named after the Tally bill** so the ERPNext document id is
  the original invoice number and reconciles directly. If a bill id clashes with an
  existing document, the invoice is auto-named instead, with a warning, and the Tally
  id stays recoverable.
- **Opening stock** posts as one Stock Reconciliation. Where Tally carries godown-wise
  and batch-wise detail, stock is placed per warehouse and per batch. A negative
  opening quantity cannot be posted and is dropped with a warning. An item with no
  rate, value, or standard cost posts at a zero valuation rate, with a warning, so its
  quantity is still recorded.

### Foreign currency

- A foreign-currency party opening posts in its own currency, against a per-currency
  control account (for example "Debtors USD" or "Creditors USD"), at Tally's recorded
  rate.
- Bill by bill when the file carries per-bill foreign amounts; otherwise as a single
  consolidated invoice (with a note that bill-level detail was not available).
- Foreign-currency advances and credits are not migrated in this version; they are
  flagged for you to enter manually.

---

## Reference: every check, warning, and error

This is the complete list of the messages Tally Migrator can show, what each means,
and what to do.

### Data-quality checks

These appear on the Check step and in the log's data-quality section.

| Code | Severity | What it means | What to do |
|---|---|---|---|
| **Invalid GSTIN** | Error | The GST number fails its format or checksum | Correct the GSTIN in Tally (or in the inline editor), or clear it to migrate the party as Unregistered |
| **Item code collision** | Error | Two Tally items reduce to the same ERPNext item code after truncation and "/" replacement | Rename one item in Tally; ERPNext item codes must be unique |
| **Imports without a GST state** | Warning | An Indian party has no state, and none could be derived from a GSTIN | Add a state so its GST invoices compute CGST/SGST vs IGST correctly, or leave it; the party still imports |
| **GSTIN and state to verify** | Warning | The GSTIN's state code maps to a different state than the ledger state | Check it; the wrong state flips CGST/SGST vs IGST |
| **PIN and state to verify** | Warning | The PIN code's region does not match the state | Check it; the PIN or the state may be a typo |
| **Imports without the email** | Warning | The email is not a valid address; the party's contact would lose it | Fix or clear the email; the party imports with its other contact details either way |
| **Imports without HSN** | Warning | An item has no HSN/SAC code | Add an HSN when ready; needed only for GST-compliant invoices |
| **Possible duplicate to review** | Warning | Two or more parties look like the same real entity | Each imports as its own party - never merged or dropped; merge them yourself in ERPNext if they are the same |
| **Will merge into one** | Warning | Two items, warehouses, or units share a name and will merge into one record on import | Rename in Tally first if that is not intended |
| **Hierarchy loop simplified** | Warning | A record's parent chain forms a loop, so its hierarchy cannot be built faithfully | Fix the parent loop in Tally if the hierarchy matters |

### Company readiness checks

These appear on the Check step when the target company is not fully set up.

**Blockers (the run is stopped until fixed):**

| Message | What to do |
|---|---|
| Default Customer Group "Commercial" is missing | Create a leaf (non-group) Customer Group named "Commercial" |
| Customer Group "Commercial" is a group node | Make it a non-group (leaf) Customer Group; ERPNext will not accept a group node on a customer |
| Default Supplier Group "All Supplier Groups" is missing | Create a Supplier Group with that name |
| Default Item Group "All Item Groups" is missing | Create an Item Group with that name |
| Default Territory "All Territories" is missing | Create a Territory with that name |
| No ERPNext company selected / company does not exist | Pick or create the company that will receive the records |

**Warnings (masters still import; part of the migration is degraded):**

| Message | Effect |
|---|---|
| Company has no default Receivable account | Customer opening balances are skipped |
| Company has no default Payable account | Supplier opening balances are skipped |
| No Fiscal Year covers today's date | The opening Journal Entry and Stock Reconciliation cannot post |
| Root warehouse not found | Opening stock may have nowhere to land |
| Opening-balance date is on or before the accounts-frozen date | The opening entries would be rejected; pick a later date or clear the frozen date |
| No Fiscal Year covers the opening-balance date | Create a fiscal year that includes the date, or pick one inside an existing year |

### Common warnings during import

These appear in the Migration Log after a run. They mean a record imported but a
dependent piece was dropped, or a setting was changed. None of them stop the
migration.

- **"address not created" / "additional address not created"** - an address could
  not be saved (often a data issue). The party still imported.
- **"address imported without its PIN code"** - the PIN did not match the state, so
  it was dropped to keep the address. Set the PIN in ERPNext.
- **"contact not created" / "additional contact not created"** - a contact could not
  be saved. The party still imported.
- **"bank account not created"** - usually because the ledger had no bank name. The
  party or account still imported.
- **"reused existing account ... and converted it to a group"** - a Tally group had
  the same name as an account ERPNext already ships as a ledger (for example a
  "Office Equipment" or "TDS Payable" group over the standard ledger of that name).
  The existing ledger was promoted to a group so the Tally sub-accounts under it could
  be created. Nothing for you to do.
- **"item not imported - its code collides with ..."** - an item code collision (see
  above). Rename one item and re-run.
- **"GST type ... not recognised - item imported as taxable"** - set the item's GST
  treatment manually if needed.
- **"no GST Item Tax Template for X% ..."** - the item imported without a tax rate;
  create the matching "GST X%" template or set the rate on the item.
- **"GST details ... India Compliance app is not installed"** - your items carry GST
  data but there is no app to store it. Install India Compliance and re-run to bring
  it over; everything else imported normally.
- **"BOM skipped - no usable Component rows"** or a component skipped - see the BOM
  edge cases above.
- **"opening stock not posted - Tally reports a negative opening quantity"** - set
  this item's opening stock manually.
- **"opening stock posted with a zero valuation rate"** - Tally carried no value for
  the item; set a valuation rate in ERPNext if it should carry value.
- **"opening stock value does not reconcile"** - Tally's recorded value did not match
  quantity times rate; the opening rate was used. Verify in Tally.
- **"... is held in Temporary Opening to keep your books balanced"** - normal; see
  the Temporary Opening explanation.
- **"opening balance skipped - account ... exists as a group account"** - the Tally
  ledger had sub-accounts, so it was created as a group, and ERPNext does not let a
  group account carry a balance. Its opening amount is held in Temporary Opening
  instead; the rest of that account class still posts normally.
- **"this company already has an Opening Entry that was not created by this
  migrator"** - opening balances were skipped to avoid double-posting against a
  manually set-up book. Cancel that entry first if you want this migration to post
  them.
- **"enabled Stock Settings > Activate Serial and Batch No for Item"** - this was
  required to post batch-tracked opening stock. Leave it on.
- **"auto-created a Tally Imported UOM Category"** - required to hold compound-unit
  conversions, because ERPNext needs every conversion to belong to a category.

---

## Settings the migration may switch on

The app never deletes or overwrites your data, but to import certain things it may
flip a few settings. Each change is recorded in the Migration Log.

- **GST HSN validation** is temporarily suspended during item import (then restored),
  so items with no HSN still land. Your site's compliance posture is not permanently
  relaxed.
- **Stock Settings > Activate Serial and Batch No for Item** is turned on if you have
  batch-tracked opening stock, because ERPNext requires it to post batch stock.
- **GST Settings UQC map** gets new rows appended for your imported units, so GST
  returns have the right unit codes.
- A **foreign-currency party's default currency** is set on the customer or supplier,
  so its opening invoice can post in that currency.

---

## Troubleshooting and FAQ

**Step 1 shows 0 customers and 0 suppliers.** You exported the wrong scope from
Tally. Re-export with **Type of master = All Masters** (see
[Exporting your data from Tally](#exporting-your-data-from-tally)).

**Step 1 says "chart-of-accounts-only export".** Your file has accounts but no
parties or items. If that is intended, continue. If not, re-export with **All
Masters**.

**The Preview shows "No bill detail" for my parties.** Bill-wise details were off in
Tally for those parties, so Tally only stored a combined balance. Each gets one
opening invoice for its full balance. This is fine.

**The Preview shows some parties under "Bills didn't reconcile".** Their individual
bills did not add up to the ledger opening. The gap posts as "On Account" so the
trial balance still ties. Check those parties in Tally for a missing or mis-dated
bill.

**The Check step blocks me with a readiness error.** A prerequisite is missing in
your ERPNext company (a default group, territory, and so on). Create what it names,
then click **Re-check**.

**My GST data did not come over.** Install the **India Compliance** app, then re-run
the migration. Without it, ERPNext has no fields to store GST details, so they are
skipped (and the log says so).

**A big amount sits in Temporary Opening.** That is normal. It is the part of your
Tally opening that does not balance on its own, plus any income/expense openings
ERPNext cannot carry. Clear it as you finish your opening entries.

**The run failed partway.** Records imported before the failure are kept. Run it
again (or use **Re-run from Source File** on the log). Already-imported records are
skipped, so only the rest is retried.

**I imported the wrong file / wrong company.** Open the Migration Log, use
**Actions > Undo This Migration**, and confirm. Only this run's records are removed.

**Can I import two companies?** Yes. Import each Tally export into its own ERPNext
company. Importing several different exports into one company makes the
reconciliation column a combined total (the app explains this when it happens).

**Is my Tally file changed?** No. The file is read only. Any fixes you make on the
Check step apply to the imported data only and are recorded in the log.

---

## Glossary

- **Master / masters** - the reference records in Tally: accounts, parties, items,
  units, groups, warehouses. As opposed to transactions.
- **Opening balance** - the balance a ledger, party, or item carries forward from the
  previous period. Tally Migrator imports these as opening entries.
- **Opening invoice** - a one-line invoice marked as an opening, used to bring a
  customer's or supplier's outstanding bill into ERPNext so future payments
  reconcile against it.
- **Temporary Opening** - the ERPNext account that holds the opening-balance
  difference so the books stay balanced. A non-zero balance is normal.
- **Trial balance** - the list of all account balances, with total debits equal to
  total credits. The reconciliation report compares Tally's and ERPNext's opening
  trial balances.
- **Reserved (account)** - classified by a standard, named Tally group;
  high-confidence.
- **Inferred (account)** - classified from a custom group's nature; worth confirming.
- **UOM** - unit of measure (Nos, Kg, Dozen, and so on).
- **UQC** - the GST Unit Quantity Code that GST returns require for each unit.
- **HSN / SAC** - the GST classification code for goods (HSN) or services (SAC).
- **GSTIN** - a party's GST registration number.
- **Idempotent** - safe to run more than once; re-running skips what already exists.
- **Reconciliation** - confirming that the figures in ERPNext match what Tally said.
- **Revert / undo** - deleting the records a migration created, to roll it back.

---

## License

GNU General Public License v3.0 (GPLv3). Copyright (c) Frappe Technologies Pvt. Ltd. and contributors.
