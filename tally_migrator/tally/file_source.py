from __future__ import annotations

import io
import re
import xml.etree.ElementTree as ET

import frappe

# The Tally master record elements we extract. The streaming parser keeps only
# these (every other element - TALLYMESSAGE wrappers, COMPANY headers, voucher
# data when present - is discarded as it is parsed), so peak memory stays
# proportional to the records we actually use rather than the whole document.
# Normalised the same way ``get_collection`` derives a tag from an obj_type
# (``upper().replace(" ", "")``), so "Stock Item" → STOCKITEM, etc.
MASTER_RECORD_TAGS = (
    "GROUP", "LEDGER", "STOCKITEM", "GODOWN",
    "COSTCENTRE", "STOCKGROUP", "UNIT", "CURRENCY",
)


# Real Tally Prime masters exports are UTF-16 (with a BOM) and contain XML-1.0
# illegal control characters - both as literal bytes and as numeric character
# references like ``&#4;`` (Tally prefixes "Not Applicable" values with one).
# Python's XML parser rejects either, so a genuine export would otherwise fail to
# import outright. These helpers normalise an upload before parsing.

# Control chars XML 1.0 forbids (everything < 0x20 except TAB/LF/CR).
_ILLEGAL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_CHAR_REF = re.compile(r"&#(x[0-9a-fA-F]+|\d+);")
# A DTD or custom entity declaration - the vector for entity-expansion
# ("billion laughs") denial-of-service against the parser.
_DTD_DECL = re.compile(r"<!(DOCTYPE|ENTITY)\b", re.IGNORECASE)


def _select_iterparse():
    """Prefer defusedxml's iterparse (rejects DTDs/entities at the *parser* level)
    when available and compatible, else fall back to the stdlib parser.

    The ``reject_unsafe_xml`` regex already neutralises entity-expansion attacks
    before parsing, so this is defense-in-depth, not the sole guard. defusedxml's
    iterparse signature has differed across versions, so we probe it once at import
    against our exact usage (file object + start/end events) and only adopt it if
    the probe parses cleanly - guaranteeing we never break the real parse path."""
    try:
        from defusedxml.ElementTree import iterparse as safe
    except Exception:
        return ET.iterparse
    try:
        for _ in safe(io.StringIO("<r><a/></r>"), events=("start", "end")):
            pass
        return safe
    except Exception:
        return ET.iterparse


_ITERPARSE = _select_iterparse()


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


def reject_unsafe_xml(text: str) -> None:
    """Refuse a document carrying a DTD or custom entity declaration.

    A genuine Tally masters export never declares a DOCTYPE or entities, so a file
    that does is either corrupt or a crafted entity-expansion payload aimed at
    exhausting the worker. Python's stdlib ElementTree has no protection against
    such expansion, so we reject the input outright rather than parse it.
    """
    if _DTD_DECL.search(text or ""):
        frappe.throw(
            "Uploaded file contains an XML DOCTYPE or entity declaration, which a "
            "genuine Tally Masters export never does. It was rejected as unsafe. "
            "Re-export the masters from Tally (XML format) and try again."
        )


class FileTallySource:
    """Offline master source: reads a Tally Prime *Masters* XML export.

    Duck-types the single method ``TallyExtractor`` depends on -
    ``get_collection(obj_type, fields)`` - so it drops into the existing
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

    def __init__(self, xml_text: str, record_tags=MASTER_RECORD_TAGS):
        cleaned = sanitize_tally_xml(xml_text)
        reject_unsafe_xml(cleaned)
        self._keep = {t.upper().replace(" ", "") for t in record_tags}
        # Stream the document once, retaining only the master record elements
        # (bucketed by tag) and dropping everything else as it is parsed. Avoids
        # building - and holding - the whole DOM the way ET.fromstring would.
        self._by_tag = self._stream_records(cleaned, self._keep)
        # extract_all() and extract_coa() both ask for the Group and Ledger
        # collections, so memoise each (obj_type, fields) result to avoid
        # re-walking the retained records on every call.
        self._collection_cache: dict = {}

    @staticmethod
    def _stream_records(cleaned: str, keep: set) -> dict:
        """Single streaming pass → ``{TAG: [element, ...]}`` for kept record tags.

        Uses ``iterparse`` and clears the parser's root after each captured record
        so already-seen wrappers/chrome are freed immediately; the captured record
        survives because the bucket holds a direct reference to it. Peak memory is
        therefore bounded by the retained records plus one in-progress subtree,
        not the size of the whole file.
        """
        buckets: dict = {tag: [] for tag in keep}
        try:
            context = _ITERPARSE(io.StringIO(cleaned), events=("start", "end"))
            _, root = next(context)   # grab the root so we can clear it as we go
            for event, elem in context:
                if event != "end":
                    continue
                if elem.tag.split("}")[-1].upper() in keep:
                    buckets.setdefault(elem.tag.split("}")[-1].upper(), []).append(elem)
                    # Drop everything iterparse has accumulated on root so far; the
                    # record just captured is safe (referenced from its bucket).
                    root.clear()
            root.clear()
        except ET.ParseError as e:
            frappe.throw(f"Uploaded file is not valid Tally XML: {e}")
        except StopIteration:
            frappe.throw("Uploaded file is empty or not valid Tally XML.")
        return buckets

    # The single method TallyExtractor depends on.
    def get_collection(self, obj_type: str, fields: list[str],
                       tag_map: dict | None = None) -> list[dict]:
        """Read one Tally object type into ``[{_name, <field>: value, ...}]``.

        For each requested field the tag(s) to read are, in order:
          - ``tag_map[field]`` when given - the exact tag name(s) a real Tally
            Prime export emits, including nested ``.LIST`` paths (e.g.
            ``LEDSTATENAME`` for state, ``ADDRESS.LIST/ADDRESS`` for the address);
          - otherwise the field name itself (``FIELD.upper()``), which for most
            fields already equals the real Tally tag (NAME, PARENT, PINCODE, …).
        The first candidate that yields a value wins.
        """
        cache_key = (obj_type, tuple(fields))
        # Hand out a fresh shallow copy of each record on every call. The source is
        # cached across requests (api._SOURCE_CACHE) and a consumer
        # (migration.overrides.apply_record_overrides) patches record dicts IN PLACE;
        # returning the cached dicts themselves would let one request's inline fixes
        # poison the parse for the next, so a re-run's override looks "already
        # applied" (old == new) and silently drops out of the migration log's edit
        # audit. Values are flat strings, so a shallow copy fully isolates them while
        # the expensive XML parse stays cached.
        cached = self._collection_cache.get(cache_key)
        if cached is not None:
            return [dict(r) for r in cached]
        tag = obj_type.upper().replace(" ", "")
        tag_map = tag_map or {}
        records: list[dict] = []
        for elem in self._by_tag.get(tag, ()):
            name = (elem.get("NAME") or elem.findtext("NAME") or "").strip()
            if not name:
                continue
            record = {"_name": name}
            for f in fields:
                candidates = tag_map.get(f) or [f.upper().replace(" ", "")]
                record[f] = self._resolve_field(elem, candidates)
            records.append(record)
        self._collection_cache[cache_key] = records
        return [dict(r) for r in records]

    def get_child_list(self, obj_type: str, child_tag: str,
                       fields: list[str]) -> list[dict]:
        """Read a *repeating* child list nested under each record of ``obj_type``.

        Unlike :meth:`get_collection` - which flattens a nested ``.LIST`` to a
        single joined value per record - this returns one dict per repeating child
        element, so bill-wise opening detail (``BILLALLOCATIONS.LIST`` under each
        ``LEDGER``) comes back as individual rows::

            [{"_parent": "ABC Company Limited", "BillDate": "20200310",
              "_name": "ABC/1", "IsAdvance": "No", "OpeningBalance": "-3000.00"},
             ...]

        Each returned row carries ``_parent`` (the owning record's NAME) and, when
        a ``NAME`` field is requested, ``_name`` (the child's own NAME) alongside
        the requested fields. ``child_tag`` is matched namespace-insensitively
        against the direct children of each record (e.g. ``"BILLALLOCATIONS.LIST"``).
        Records with no such children contribute nothing.
        """
        cache_key = ("child", obj_type, child_tag, tuple(fields))
        # Copy per call, same reason as get_collection: the cross-request parse cache
        # must never hand out dicts a consumer could mutate in place.
        cached = self._collection_cache.get(cache_key)
        if cached is not None:
            return [dict(r) for r in cached]
        tag = obj_type.upper().replace(" ", "")
        want_child = child_tag.upper().replace(" ", "")
        rows: list[dict] = []
        for elem in self._by_tag.get(tag, ()):
            parent = (elem.get("NAME") or elem.findtext("NAME") or "").strip()
            for child in elem:
                if child.tag.split("}")[-1].upper() != want_child:
                    continue
                row = {"_parent": parent}
                for f in fields:
                    value = self._resolve_field(child, [f.upper().replace(" ", "")])
                    row[f] = value
                    if f.upper() == "NAME":
                        row["_name"] = value
                rows.append(row)
        self._collection_cache[cache_key] = rows
        return [dict(r) for r in rows]

    def item_gst_rates(self) -> dict:
        """``{stock item name: combined GST rate string}`` read per duty head.

        The rate lives nested under GSTDETAILS.LIST/STATEWISEDETAILS.LIST/
        RATEDETAILS.LIST, one entry per duty head (CGST/SGST-UTGST/IGST/Cess). The
        combined GST rate is IGST (== CGST + SGST); we read by duty head rather than
        relying on tag order, so a Cess rate can never be mistaken for the GST rate.
        Empty string when the item carries no rate (nil/exempt/non-GST)."""
        out: dict = {}
        for elem in self._by_tag.get("STOCKITEM", ()):
            name = (elem.get("NAME") or elem.findtext("NAME") or "").strip()
            if not name:
                continue
            heads: dict = {}
            for node in elem.iter():
                if node.tag.split("}")[-1].upper() != "RATEDETAILS.LIST":
                    continue
                duty = rate = ""
                for ch in node:
                    local = ch.tag.split("}")[-1].upper()
                    if local == "GSTRATEDUTYHEAD":
                        duty = (ch.text or "").strip().upper()
                    elif local == "GSTRATE":
                        rate = (ch.text or "").strip()
                if duty and rate:
                    heads[duty] = rate
            igst = heads.get("IGST")
            if not igst:
                cgst = heads.get("CGST")
                sgst = heads.get("SGST/UTGST") or heads.get("SGST")
                if cgst or sgst:
                    try:
                        igst = f"{float(cgst or 0) + float(sgst or 0):g}"
                    except ValueError:
                        igst = ""
            out[name] = igst or ""
        return out

    def item_price_levels(self) -> dict:
        """``{stock item name: [{level, date, rate, discount, ending}, ...]}``.

        Reads Tally price levels from FULLPRICELIST.LIST (one per price level +
        effective DATE), each carrying PRICELEVELLIST.LIST slabs (ENDINGAT / RATE /
        DISCOUNT). Per price level we keep the latest DATE only (price revisions
        collapse to the current price) and the first slab (single-slab is the norm).
        Raw strings are returned; the importer parses rate/uom/qty."""
        out: dict = {}
        for elem in self._by_tag.get("STOCKITEM", ()):
            name = (elem.get("NAME") or elem.findtext("NAME") or "").strip()
            if not name:
                continue
            by_level: dict = {}     # level -> (date, slab dict)
            for fp in elem.iter():
                if fp.tag.split("}")[-1].upper() != "FULLPRICELIST.LIST":
                    continue
                date = level = ""
                slabs = []
                for ch in fp:
                    t = ch.tag.split("}")[-1].upper()
                    if t == "DATE":
                        date = (ch.text or "").strip()
                    elif t == "PRICELEVEL":
                        level = (ch.text or "").strip()
                    elif t == "PRICELEVELLIST.LIST":
                        row = {}
                        for c in ch:
                            ct = c.tag.split("}")[-1].upper()
                            if ct in ("ENDINGAT", "RATE", "DISCOUNT"):
                                row[ct.lower()] = (c.text or "").strip()
                        if row:
                            slabs.append(row)
                if not level or not slabs:
                    continue
                prev = by_level.get(level)
                # YYYYMMDD strings sort chronologically; keep the latest revision.
                if prev is None or date > prev[0]:
                    by_level[level] = (date, slabs[0])
            if by_level:
                out[name] = [
                    {"level": lvl, "date": d, "rate": s.get("rate", ""),
                     "discount": s.get("discount", ""), "ending": s.get("endingat", "")}
                    for lvl, (d, s) in by_level.items()
                ]
        return out

    def currency_iso_map(self) -> dict:
        """``{currency NAME (Tally symbol): ISO code}`` from the CURRENCY masters,
        e.g. ``{"$": "USD"}``. Used to resolve a forex party's CurrencyName to an
        ERPNext currency. Only entries that carry an ISOCURRENCYCODE are returned."""
        out: dict = {}
        for elem in self._by_tag.get("CURRENCY", ()):
            name = (elem.get("NAME") or elem.findtext("NAME") or "").strip()
            iso = (elem.findtext("ISOCURRENCYCODE") or "").strip()
            if name and iso:
                out[name] = iso
        return out

    def item_boms(self) -> dict:
        """``{stock item name: [{name, basic_qty, components:[...]}]}``.

        Reads Tally bills of materials from MULTICOMPONENTLIST.LIST (Tally supports
        multiple BOMs per item), each carrying COMPONENTLISTNAME, COMPONENTBASICQTY
        ("1 Nos" = qty the BOM makes) and MULTICOMPONENTITEMLIST.LIST components
        (NATUREOFITEM / STOCKITEMNAME / GODOWNNAME / ACTUALQTY "1 Ream"). The legacy
        empty COMPONENTLIST.LIST is ignored. Raw strings; the importer parses qty/uom."""
        out: dict = {}
        for elem in self._by_tag.get("STOCKITEM", ()):
            name = (elem.get("NAME") or elem.findtext("NAME") or "").strip()
            if not name:
                continue
            boms = []
            for mc in elem.iter():
                if mc.tag.split("}")[-1].upper() != "MULTICOMPONENTLIST.LIST":
                    continue
                bom_name = basic_qty = ""
                comps = []
                for ch in mc:
                    t = ch.tag.split("}")[-1].upper()
                    if t == "COMPONENTLISTNAME":
                        bom_name = (ch.text or "").strip()
                    elif t == "COMPONENTBASICQTY":
                        basic_qty = (ch.text or "").strip()
                    elif t == "MULTICOMPONENTITEMLIST.LIST":
                        row = {}
                        for c in ch:
                            ct = c.tag.split("}")[-1].upper()
                            if ct in ("NATUREOFITEM", "STOCKITEMNAME", "GODOWNNAME", "ACTUALQTY"):
                                row[ct.lower()] = (c.text or "").strip()
                        if row.get("stockitemname"):
                            comps.append(row)
                if comps:
                    boms.append({"name": bom_name or name, "basic_qty": basic_qty,
                                 "components": comps})
            if boms:
                out[name] = boms
        return out

    def item_godown_openings(self) -> dict:
        """``{stock item name: [{godown, qty, rate, value}, ...]}``.

        Tally stores an item's opening stock godown-wise (and batch-wise) under
        repeating ``BATCHALLOCATIONS.LIST`` rows, each carrying GODOWNNAME plus the
        allocation's own OPENINGBALANCE ("118 Ream") / OPENINGRATE ("265.50/Ream") /
        OPENINGVALUE ("-31329.00"). The item-level OPENINGBALANCE is their sum, so
        without this the whole opening collapses into one default warehouse. Raw
        strings are returned; the importer parses qty/rate/value and maps the godown
        to an ERPNext warehouse. Only rows that carry both a godown and an opening
        balance are returned (an empty stub contributes nothing)."""
        out: dict = {}
        for elem in self._by_tag.get("STOCKITEM", ()):
            name = (elem.get("NAME") or elem.findtext("NAME") or "").strip()
            if not name:
                continue
            rows = []
            for ba in elem:
                if ba.tag.split("}")[-1].upper() != "BATCHALLOCATIONS.LIST":
                    continue
                d = {}
                for c in ba:
                    t = c.tag.split("}")[-1].upper()
                    if t in ("GODOWNNAME", "OPENINGBALANCE", "OPENINGRATE", "OPENINGVALUE"):
                        d[t.lower()] = (c.text or "").strip()
                if d.get("godownname") and d.get("openingbalance"):
                    rows.append({
                        "godown": d["godownname"],
                        "qty": d.get("openingbalance", ""),
                        "rate": d.get("openingrate", ""),
                        "value": d.get("openingvalue", ""),
                    })
            if rows:
                out[name] = rows
        return out

    # ── Field resolution (tag overrides + nested .LIST descent) ────────────────
    @classmethod
    def _resolve_field(cls, elem, candidates: list) -> str:
        """First candidate that yields a non-empty value wins.

        A candidate is either a tag-path string (``"EMAIL"`` or
        ``"ADDRESS.LIST/ADDRESS"``) or ``{"path": ..., "join": ", "}`` to join
        repeated nodes (e.g. multi-line addresses). Single-valued paths return the
        last matched node's text - for revision lists like ``STANDARDPRICELIST``
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
        extractor's fixed FETCH list never reads - Tally UDFs and any other
        fields outside our mapping - so nothing is dropped silently.

        Returns ``{TAGNAME: {...}}`` where ``TAGNAME`` is the upper-cased local tag
        name (XML namespace stripped) and each value carries both the original
        coverage fields and the value-distribution stats the Step-2 classifier needs::

            {"count": int,        # occurrences (incl. empty) - back-compat
             "filled": int,       # occurrences with a non-empty value
             "fill_rate": float,  # filled / total records of this type (0..1)
             "distinct": int|None,# distinct non-empty values, or None when "many"
                                  #   (more than DISTINCT_CAP - i.e. high cardinality)
             "values": [str],     # up to 12 distinct example values (for Select/Bool)
             "is_udf": bool,      # tag carried a namespace/colon (Tally UDF signal)
             "sample": str, "records": [names]}

        ``distinct``/``values`` are how the classifier tells a low-cardinality
        category (Select) from a free-form identifier without any hand-listed names.
        """
        DISTINCT_CAP = 50          # beyond this we stop tracking values and call it "many"
        tag = obj_type.upper().replace(" ", "")
        records = self._by_tag.get(tag, ())
        record_total = len(records)
        out: dict = {}
        for elem in records:
            name = (elem.get("NAME") or elem.findtext("NAME") or "").strip()
            if not name:
                continue
            for child in elem:
                full = child.tag                          # may be "{TallyUDF}Field"
                local = full.split("}")[-1].upper()        # bare name (namespace dropped)
                # A Tally UDF (user-defined field) serialises in the TallyUDF XML
                # namespace (xmlns:UDF="TallyUDF"), which ElementTree renders as
                # "{TallyUDF}Field"; a built-in master field carries no namespace. We
                # must read this off the FULL tag - the namespace is gone after the
                # split above. (Detection unverified on a real UDF export - see the
                # parked-udf gate; correct by design, inert until such a file exists.)
                is_udf = full.startswith("{") and "UDF" in full.split("}")[0].upper()
                entry = out.setdefault(local, {
                    "count": 0, "filled": 0, "sample": "", "records": [],
                    "is_udf": is_udf, "_distinct": set(), "_overflow": False,
                })
                entry["count"] += 1
                text = (child.text or "").strip()
                if text:
                    entry["filled"] += 1
                    if not entry["sample"]:
                        entry["sample"] = text
                    if not entry["_overflow"] and text not in entry["_distinct"]:
                        if len(entry["_distinct"]) < DISTINCT_CAP:
                            entry["_distinct"].add(text)
                        else:
                            entry["_overflow"] = True   # high cardinality - stop tracking
                if len(entry["records"]) < 5 and name not in entry["records"]:
                    entry["records"].append(name)
        for entry in out.values():
            distinct = entry.pop("_distinct")
            overflow = entry.pop("_overflow")
            entry["distinct"] = None if overflow else len(distinct)
            entry["values"] = sorted(distinct)[:12]
            entry["fill_rate"] = (round(entry["filled"] / record_total, 3)
                                  if record_total else 0.0)
        return out

    def approx_record_count(self) -> int:
        """Total kept master records - a cheap volume signal (already parsed).

        Used to decide whether a migration is large enough to run as a background
        job instead of synchronously inside the web request."""
        return sum(len(v) for v in self._by_tag.values())

    # Callers ping() the source before extracting; a loaded file is always ready.
    def ping(self) -> bool:
        return True
