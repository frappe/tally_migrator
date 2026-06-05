import frappe

ROLE_NAME = "Tally Migration Manager"


def after_install():
    """Create the migration manager role on a fresh install (idempotent)."""
    if frappe.db.exists("Role", ROLE_NAME):
        return
    role = frappe.new_doc("Role")
    role.role_name = ROLE_NAME
    role.desk_access = 1
    role.insert(ignore_permissions=True)
    frappe.db.commit()
