import xml.etree.ElementTree as ET

import frappe


class FileTallySource:
    """Offline master source: reads a Tally Prime *Masters* XML export.

    Duck-types the single method ``TallyExtractor`` depends on —
    ``get_collection(obj_type, fields)`` — so it drops into the existing
    extraction pipeline with no change to ``TallyExtractor`` or the importers.

    Why this exists
    ---------------
    The live :class:`TallyClient` requires Tally to be open with its HTTP
    server reachable on port 9000. That is brittle for demos and impossible on
    hosted ERPNext where Tally lives on the customer's LAN. A masters XML file
    exported once from Tally (Gateway → Import/Export → Export → Masters, XML
    format) is portable and lets the entire masters migration run offline.

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

    # Mirrors TallyClient.get_collection so the extractor can't tell them apart.
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

    # Parity with TallyClient so callers can ping() uniformly. A loaded file is
    # always "reachable".
    def ping(self) -> bool:
        return True
