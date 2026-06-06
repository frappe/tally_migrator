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
