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

    def raw_tags(self, obj_type: str) -> dict:
        """Enumerate every direct child tag present on records of ``obj_type``.

        Used by the field-coverage report to find data in the file that the
        extractor's fixed FETCH list never reads — Tally UDFs and any other
        fields outside our mapping — so nothing is dropped silently.

        Returns ``{TAGNAME: {"count": int, "sample": str, "records": [names]}}``
        where ``TAGNAME`` is the upper-cased local tag name (namespace stripped,
        so a ``UDF:`` field appears under its bare name).
        """
        tag = obj_type.upper().replace(" ", "")
        out: dict = {}
        for elem in self._root.iter(tag):
            name = (elem.get("NAME") or elem.findtext("NAME") or "").strip()
            if not name:
                continue
            for child in elem:
                local = child.tag.split("}")[-1].upper()  # drop {namespace}
                entry = out.setdefault(local, {"count": 0, "sample": "", "records": []})
                entry["count"] += 1
                text = (child.text or "").strip()
                if not entry["sample"] and text:
                    entry["sample"] = text
                if len(entry["records"]) < 5 and name not in entry["records"]:
                    entry["records"].append(name)
        return out

    # Callers ping() the source before extracting; a loaded file is always ready.
    def ping(self) -> bool:
        return True
