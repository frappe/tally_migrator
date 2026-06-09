from frappe.model.document import Document


class TallyMigrationDraft(Document):
    """Per-user wizard draft. One row per user (autonamed by ``user``); the API in
    ``tally_migrator.api`` upserts/reads/clears it. No server-side logic needed."""
    pass
