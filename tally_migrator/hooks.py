app_name        = "tally_migrator"
app_title       = "Tally Migrator"
app_publisher   = "Parth Garachh"
app_description = "Migrate masters and opening balances from Tally to ERPNext"
app_email       = "parth@frappe.io"
app_license     = "MIT"
app_version     = "1.0.0"

required_apps = ["frappe", "erpnext"]

# ── Lifecycle ─────────────────────────────────────────────────────────────────
# The "Tally Migration Manager" role is created in after_install (single source
# of truth). Page/DocType permissions reference it declaratively.

after_install = "tally_migrator.install.after_install"
