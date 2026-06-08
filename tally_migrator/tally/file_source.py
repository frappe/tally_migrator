from __future__ import annotations

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
    def get_collection(self, obj_type: str, fields: list[str],
                       aliases: dict | None = None) -> list[dict]:
        """Read one Tally object type into ``[{_name, <field>: value, ...}]``.

        For each requested field the parser tries, in order:
          1. the canonical tag ``FIELD.upper()`` (matches a hand-authored export),
          2. each fallback in ``aliases[field]`` — the tag names a *real* Tally
             Prime export actually emits (e.g. ``LEDSTATENAME`` for state), and
             nested ``.LIST`` paths (e.g. ``ADDRESS.LIST/ADDRESS``).
        The first candidate that yields a value wins, so genuine Tally output and
        the legacy flat-tag sample both import without per-field guesswork.
        """
        tag = obj_type.upper().replace(" ", "")
        aliases = aliases or {}
        records: list[dict] = []
        for elem in self._root.iter(tag):
            name = (elem.get("NAME") or elem.findtext("NAME") or "").strip()
            if not name:
                continue
            record = {"_name": name}
            for f in fields:
                candidates = [f.upper().replace(" ", ""), *aliases.get(f, [])]
                record[f] = self._resolve_field(elem, candidates)
            records.append(record)
        return records

    # ── Field resolution (tag aliases + nested .LIST descent) ──────────────────
    @classmethod
    def _resolve_field(cls, elem, candidates: list) -> str:
        """First candidate that yields a non-empty value wins.

        A candidate is either a tag-path string (``"EMAIL"`` or
        ``"ADDRESS.LIST/ADDRESS"``) or ``{"path": ..., "join": ", "}`` to join
        repeated nodes (e.g. multi-line addresses). Single-valued paths return the
        last matched node's text — for revision lists like ``STANDARDPRICELIST``
        that is the most recent value."""
        for cand in candidates:
            path = cand["path"] if isinstance(cand, dict) else cand
            join = cand.get("join") if isinstance(cand, dict) else None
            texts = cls._collect_texts(elem, path)
            if texts:
                return join.join(texts) if join is not None else texts[-1]
        return ""

    @staticmethod
    def _collect_texts(elem, path: str) -> list[str]:
        """Walk a ``/``-separated tag path (namespace-insensitive) and return the
        stripped, non-empty text of every node reached at the final step."""
        nodes = [elem]
        for part in path.split("/"):
            want = part.upper()
            nodes = [child for n in nodes for child in n
                     if child.tag.split("}")[-1].upper() == want]
        return [t for n in nodes if (t := (n.text or "").strip())]

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
