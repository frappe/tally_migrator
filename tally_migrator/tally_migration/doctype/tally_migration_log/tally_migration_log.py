import frappe
from frappe.model.document import Document


class TallyMigrationLog(Document):
    # begin: auto-generated types
    # This code is auto-generated. Do not modify anything in this block.

    from frappe.types import DF
    from tally_migrator.tally_migration.doctype.tally_migration_error.tally_migration_error import (
        TallyMigrationError,
    )

    applied_edits: DF.Code | None
    company: DF.Link
    coverage_report: DF.Code | None
    error_log: DF.LongText | None
    errors: DF.Table[TallyMigrationError]
    extracted_counts: DF.Code | None
    import_summary: DF.Code | None
    migration_date: DF.Datetime | None
    migration_type: DF.Literal["Masters", "Transactions"]
    source_file: DF.Attach | None
    status: DF.Literal["", "Running", "Completed", "Completed with Errors", "Failed"]
    tally_company: DF.Data
    validation_report: DF.Code | None
    # end: auto-generated types

    def before_insert(self):
        if not self.migration_date:
            self.migration_date = frappe.utils.now_datetime()
