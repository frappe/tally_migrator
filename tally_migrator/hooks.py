app_name        = "tally_migrator"
app_title       = "Tally Migrator"
app_publisher   = "Parth Garachh"
app_description = "Migrate masters and opening balances from Tally to ERPNext"
app_email       = "parth@frappe.io"
app_license     = "GNU General Public License (v3)"
app_version     = "1.0.0"

required_apps = ["frappe", "erpnext"]

# ── Lifecycle ─────────────────────────────────────────────────────────────────
# The "Tally Migration Manager" role is created in after_install (single source
# of truth). Page/DocType permissions reference it declaratively.

after_install = "tally_migrator.install.after_install"

# ── Scheduler ─────────────────────────────────────────────────────────────────
# Auto-resume a run that a per-record hang hard-killed: the guard leaves an in-flight
# marker, this sweep re-enqueues the run so it steps past the culprit. Idempotent and
# capped (see resume.py / record_guard.py), so it is safe to run frequently.
scheduler_events = {
    "cron": {
        "*/5 * * * *": ["tally_migrator.migration.resume.resume_stalled_runs"],
    }
}
