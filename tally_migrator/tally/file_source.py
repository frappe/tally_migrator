import xml.etree.ElementTree as ET

import frappe


class FileTallySource:
    """Offline master source: reads a Tally Prime *Masters* XML export.

    Duck-types the single method ``TallyExtractor`` depends on —
    ``get_collection(obj_type, fields)`` — so it drops into the existing
    extraction pipeline with no change to ``TallyExtractor`` or the importers.

    Why a file
    ----------
    Hosted ERPNext (e.g. Frappe Cloud) cannot reach a Tally instance on the
    customer's LAN, so a live connection is impossible. A masters XML file
    exported once from Tally (Gateway → Import/Export → Export → Masters, XML
    format) is portable and lets the entire masters migration run anywhere.

    The on-disk export wraps each record in a ``<TALLYMESSAGE>`` containing a
    ``<LEDGER>`` / ``<GROUP>`` / ``<STOCKITEM>`` / ``<GODOWN>`` element. The
    record name is the ``NAME`` attribute and the FETCH fields map to child
    tags with the same uppercase/no-space convention the live collection uses,
    so the same tag derivation works for both sources.
    """

    def __init__(self, xml_text: str):
        try:
            self._root = ET.fromstring(xml_text)
        except ET.ParseError as e:
            frappe.throw(f"Uploaded file is not valid Tally XML: {e}")

    # The single method TallyExtractor depends on.
    def get_collection(self, obj_type: str, fields: list[str]) -> list[dict]:
        tag = obj_type.upper().replace(" ", "")
        records: list[dict] = []
        for elem in self._root.iter(tag):
            name = (elem.get("NAME") or elem.findtext("NAME") or "").strip()
            if not name:
                continue
            record = {"_name": name}
            for f in fields:
                child = elem.findtext(f.upper().replace(" ", "")) or ""
                record[f] = child.strip()
            records.append(record)
        return records

    # Callers ping() the source before extracting; a loaded file is always ready.
    def ping(self) -> bool:
        return True
