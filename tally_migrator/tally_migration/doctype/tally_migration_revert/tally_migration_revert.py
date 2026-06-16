import frappe
from frappe.model.document import Document


class TallyMigrationRevert(Document):
    """Audit record of one 'Undo This Migration' action.

    Created by tally_migrator.migration.rollback.revert_migration. Read-only by
    design: it records what a revert deleted and what it had to keep, so the heavy
    per-revert detail stays off the Tally Migration Log (which only flips its
    status to 'Reverted'). Part of the optional rollback feature - delete this
    doctype to remove that feature; nothing in the import pipeline depends on it.
    """

    pass
