import frappe
from frappe.model.document import Document


class TallyMigrationRevertItem(Document):
    """One row of an 'Undo This Migration' result: a document that was deleted, or
    kept (with the reason it could not be deleted). Part of the optional rollback
    feature - delete the Tally Migration Revert doctype to remove it."""

    pass
