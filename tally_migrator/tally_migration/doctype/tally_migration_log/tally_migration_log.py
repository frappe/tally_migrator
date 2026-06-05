import frappe
from frappe.model.document import Document


class TallyMigrationLog(Document):
    # begin: auto-generated types
    # This code is auto-generated. Do not modify anything in this block.

    from frappe.types import DF

    company: DF.Link
    error_log: DF.LongText | None
    extracted_counts: DF.Code | None
    import_summary: DF.Code | None
    migration_date: DF.Datetime | None
    migration_type: DF.Literal["Masters", "Transactions"]
    status: DF.Literal["", "Running", "Completed", "Completed with Errors", "Failed"]
    tally_company: DF.Data
    # end: auto-generated types

    def before_insert(self):
        if not self.migration_date:
            self.migration_date = frappe.utils.now_datetime()
