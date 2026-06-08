from __future__ import annotations

import re
import xml.etree.ElementTree as ET

import frappe


# Real Tally Prime masters exports are UTF-16 (with a BOM) and contain XML-1.0
# illegal control characters — both as literal bytes and as numeric character
# references like ``&#4;`` (Tally prefixes "Not Applicable" values with one).
# Python's XML parser rejects either, so a genuine export would otherwise fail to
# import outright. These helpers normalise an upload before parsing.

# Control chars XML 1.0 forbids (everything < 0x20 except TAB/LF/CR).
_ILLEGAL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_CHAR_REF = re.compile(r"&#(x[0-9a-fA-F]+|\d+);")


def decode_tally_bytes(raw) -> str:
    """Decode raw upload bytes to text, honouring Tally's UTF-16 export encoding.

    Detects the byte-order mark (UTF-16 LE/BE or a UTF-8 BOM); falls back to
    UTF-8, then UTF-16 / latin-1 for headerless oddities. A str passes through.
    """
    if not isinstance(raw, (bytes, bytearray)):
        return raw
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16")
    if raw[:3] == b"\xef\xbb\xbf":
        return raw.decode("utf-8-sig")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        # Headerless UTF-16 shows interleaved NULs; otherwise fall back to latin-1.
        if b"\x00" in raw[:64]:
            return raw.decode("utf-16", errors="replace")
        return raw.decode("latin-1")


def _drop_illegal_ref(match: "re.Match") -> str:
    body = match.group(1)
    cp = int(body[1:], 16) if body[0] in "xX" else int(body)
    legal = cp in (0x09, 0x0A, 0x0D) or 0x20 <= cp <= 0xD7FF or 0xE000 <= cp <= 0xFFFD
    return match.group(0) if legal else ""


def sanitize_tally_xml(text: str) -> str:
    """Strip XML-1.0-illegal characters and numeric refs Tally emits, plus a
    leading BOM, so ElementTree can parse a genuine export."""
    if text and text[0] == "﻿":
        text = text[1:]
    text = _CHAR_REF.sub(_drop_illegal_ref, text)
    return _ILLEGAL_CHARS.sub("", text)


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
            self._root = ET.fromstring(sanitize_tally_xml(xml_text))
        except ET.ParseError as e:
            frappe.throw(f"Uploaded file is not valid Tally XML: {e}")

    # The single method TallyExtractor depends on.
    def get_collection(self, obj_type: str, fields: list[str],
                       tag_map: dict | None = None) -> list[dict]:
        """Read one Tally object type into ``[{_name, <field>: value, ...}]``.

        For each requested field the tag(s) to read are, in order:
          - ``tag_map[field]`` when given — the exact tag name(s) a real Tally
            Prime export emits, including nested ``.LIST`` paths (e.g.
            ``LEDSTATENAME`` for state, ``ADDRESS.LIST/ADDRESS`` for the address);
          - otherwise the field name itself (``FIELD.upper()``), which for most
            fields already equals the real Tally tag (NAME, PARENT, PINCODE, …).
        The first candidate that yields a value wins.
        """
        tag = obj_type.upper().replace(" ", "")
        tag_map = tag_map or {}
        records: list[dict] = []
        for elem in self._root.iter(tag):
            name = (elem.get("NAME") or elem.findtext("NAME") or "").strip()
            if not name:
                continue
            record = {"_name": name}
            for f in fields:
                candidates = tag_map.get(f) or [f.upper().replace(" ", "")]
                record[f] = self._resolve_field(elem, candidates)
            records.append(record)
        return records

    # ── Field resolution (tag overrides + nested .LIST descent) ────────────────
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
