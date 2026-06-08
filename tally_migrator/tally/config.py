from dataclasses import dataclass


@dataclass
class TallyConfig:
    """Configuration for a masters migration run.

    The migrator reads from an uploaded Tally *Masters* XML export, so the
    only inputs that matter are which Tally company the file came from (for the
    audit log) and which ERPNext company should receive the data.
    """

    tally_company: str = ""    # Source label, e.g. the export file name
    erpnext_company: str = ""  # Target Company inside ERPNext
    source_file: str = ""      # file_url of the uploaded XML, stored on the log for re-runs
    validation_report: str = ""  # JSON of the pre-flight data-quality report, stored on the log
    coverage_report: str = ""  # JSON of the field-coverage report (unmapped fields), stored on the log
    coa_mode: str = "reuse"    # Chart of Accounts: "reuse" ERPNext defaults | "mirror" Tally tree
