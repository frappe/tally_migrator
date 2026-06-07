from frappe.model.document import Document


class TallyMigrationError(Document):
    # begin: auto-generated types
    # This code is auto-generated. Do not modify anything in this block.

    from frappe.types import DF

    record_name: DF.Data | None
    record_type: DF.Data | None
    reason: DF.SmallText | None
    # end: auto-generated types

    pass
