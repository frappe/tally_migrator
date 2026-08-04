"""HSN resolved from a shared GST Classification master (#4).

Tally lets a book define an HSN once on a GST Classification master and have many
stock items reference it by name (``HSNDETAILS.LIST/HSNCLASSIFICATIONNAME``, with
``SRCOFHSNDETAILS = "Use GST Classification"``). Those items carry no HSN of their
own, so without resolution they import blank. All values here are illustrative.

Two layers:
  * end-to-end through ``FileTallySource`` (proves GSTCLASSIFICATION is indexed by the
    streamer and the nested tag paths resolve), and
  * ``_attach_item_hsn_from_classification`` / ``_gst_classification_hsn`` logic against
    a mock client (every edge case, isolated).
"""
import unittest

from tally_migrator.tally.extractors import TallyExtractor, _norm_hsn_class_name
from tally_migrator.tally.file_source import FileTallySource
from tally_migrator.validation.engine import validate_masters


# ── End-to-end through the real parser ────────────────────────────────────────

def _item_xml(name, inner):
    return (f'<TALLYMESSAGE><STOCKITEM NAME="{name}"><NAME>{name}</NAME>'
            f'<BASEUNITS>Nos</BASEUNITS>{inner}</STOCKITEM></TALLYMESSAGE>')


_XML = (
    '<ENVELOPE><BODY><IMPORTDATA><REQUESTDATA>'
    # A GST Classification master carrying the HSN once.
    '<TALLYMESSAGE><GSTCLASSIFICATION NAME="HSN 8471 - Computers">'
    '<HSNDETAILS.LIST><HSNCODE>8471</HSNCODE>'
    '<SRCOFHSNDETAILS>Specify Details Here</SRCOFHSNDETAILS></HSNDETAILS.LIST>'
    '</GSTCLASSIFICATION></TALLYMESSAGE>'
    # Item that points at the classification (no HSN of its own).
    + _item_xml("Laptop",
                '<HSNDETAILS.LIST>'
                '<HSNCLASSIFICATIONNAME>HSN 8471 - Computers</HSNCLASSIFICATIONNAME>'
                '<SRCOFHSNDETAILS>Use GST Classification</SRCOFHSNDETAILS></HSNDETAILS.LIST>')
    # Item with its own HSN (control - must win / be untouched).
    + _item_xml("Mouse",
                '<HSNDETAILS.LIST><HSNCODE>8544</HSNCODE>'
                '<SRCOFHSNDETAILS>Specify Details Here</SRCOFHSNDETAILS></HSNDETAILS.LIST>')
    # Item with no HSN at all - must stay blank.
    + _item_xml("Pencil", "")
    + '</REQUESTDATA></IMPORTDATA></BODY></ENVELOPE>'
)


class TestHsnFromClassificationEndToEnd(unittest.TestCase):
    def setUp(self):
        self.items = {it["_name"]: it
                      for it in TallyExtractor(FileTallySource(_XML)).extract_all().items}

    def test_item_hsn_resolved_from_classification(self):
        # Laptop had no HSN of its own; it inherits 8471 from the classification it names.
        self.assertEqual(self.items["Laptop"]["HSNCode"], "8471")

    def test_item_own_hsn_is_preserved(self):
        self.assertEqual(self.items["Mouse"]["HSNCode"], "8544")

    def test_item_with_no_hsn_stays_blank(self):
        self.assertEqual((self.items["Pencil"].get("HSNCode") or ""), "")

    def test_streamer_indexes_gst_classification(self):
        # If GSTCLASSIFICATION were not in MASTER_RECORD_TAGS the map would be empty
        # and Laptop would be blank - this is the guard against that wiring regressing.
        cmap = TallyExtractor(FileTallySource(_XML))._gst_classification_hsn()
        self.assertEqual(cmap, {"hsn 8471 - computers": "8471"})

    def test_validation_no_false_hsn_warning_for_resolved_item(self):
        masters = TallyExtractor(FileTallySource(_XML)).extract_all()
        codes = [(i.entity_name, i.code) for i in validate_masters(masters).issues]
        missing = {name for name, code in codes if code == "HSN_MISSING"}
        self.assertNotIn("Laptop", missing)   # resolved -> no warning
        self.assertNotIn("Mouse", missing)    # own HSN -> no warning
        self.assertIn("Pencil", missing)      # genuinely blank -> still warned


# ── Logic edge cases against a mock client ────────────────────────────────────

class _ClsClient:
    """Minimal client: serves GST Classification rows to get_collection, nothing else."""
    def __init__(self, classifications=(), raise_on_read=False):
        self._c = classifications           # iterable of (name, hsn)
        self._raise = raise_on_read

    def get_collection(self, obj_type, fields, tag_map=None):
        if self._raise:
            raise RuntimeError("source cannot supply this collection")
        if obj_type == "GST Classification":
            return [{"_name": n, "HSNCode": h} for n, h in self._c]
        return []


def _ext(classifications=(), raise_on_read=False):
    return TallyExtractor(_ClsClient(classifications, raise_on_read))


def _item(name, hsn="", cls="", src=""):
    return {"_name": name, "HSNCode": hsn,
            "HSNClassificationName": cls, "SrcOfHsnDetails": src}


class TestAttachHsnFromClassification(unittest.TestCase):
    CLS = [("HSN 8471 - Computers", "8471"), ("SAC 9983 - Consulting", "9983")]

    def _run(self, items, classifications=None):
        ext = _ext(self.CLS if classifications is None else classifications)
        ext._attach_item_hsn_from_classification(items)
        return items

    def test_blank_item_inherits_classification_hsn(self):
        it = self._run([_item("A", cls="HSN 8471 - Computers", src="Use GST Classification")])[0]
        self.assertEqual(it["HSNCode"], "8471")

    def test_own_hsn_always_wins(self):
        it = self._run([_item("A", hsn="1234", cls="HSN 8471 - Computers",
                              src="Use GST Classification")])[0]
        self.assertEqual(it["HSNCode"], "1234")

    def test_specify_details_here_with_blank_own_stays_blank(self):
        # User chose to type their own HSN and left it empty: a stale link name must
        # not be pulled in.
        it = self._run([_item("A", hsn="", cls="HSN 8471 - Computers",
                              src="Specify Details Here")])[0]
        self.assertEqual(it["HSNCode"], "")

    def test_missing_classification_leaves_item_blank(self):
        it = self._run([_item("A", cls="HSN 9999 - Unknown", src="Use GST Classification")])[0]
        self.assertEqual(it["HSNCode"], "")

    def test_item_without_a_classification_name_is_untouched(self):
        it = self._run([_item("A")])[0]
        self.assertEqual(it["HSNCode"], "")

    def test_name_match_is_case_and_space_insensitive(self):
        it = self._run([_item("A", cls="  hsn   8471 - COMPUTERS ",
                              src="Use GST Classification")])[0]
        self.assertEqual(it["HSNCode"], "8471")

    def test_sac_classification_resolves_too(self):
        it = self._run([_item("A", cls="SAC 9983 - Consulting", src="Use GST Classification")])[0]
        self.assertEqual(it["HSNCode"], "9983")

    def test_no_classifications_is_a_noop(self):
        items = [_item("A", cls="HSN 8471 - Computers", src="Use GST Classification"),
                 _item("B", hsn="5555")]
        self._run(items, classifications=[])
        self.assertEqual([i["HSNCode"] for i in items], ["", "5555"])


class TestGstClassificationHsnMap(unittest.TestCase):
    def test_map_normalises_names_and_drops_hsnless(self):
        ext = _ext([("HSN 8471 - Computers", "8471"),
                    ("Rate Only Class", ""),           # no HSN -> excluded
                    ("SAC 9983 - Consulting", " 9983 ")])
        self.assertEqual(ext._gst_classification_hsn(),
                         {"hsn 8471 - computers": "8471",
                          "sac 9983 - consulting": "9983"})

    def test_duplicate_names_last_wins(self):
        ext = _ext([("Dup", "1111"), ("Dup", "2222")])
        self.assertEqual(ext._gst_classification_hsn(), {"dup": "2222"})

    def test_source_error_yields_empty_map_not_raise(self):
        # A live client that can't supply the collection must degrade to a no-op,
        # never break extraction.
        self.assertEqual(_ext(raise_on_read=True)._gst_classification_hsn(), {})


class TestNormaliseHsnClassName(unittest.TestCase):
    def test_collapses_space_and_casefolds(self):
        self.assertEqual(_norm_hsn_class_name("  HSN   8471 - Computers "),
                         "hsn 8471 - computers")

    def test_blank_and_none_safe(self):
        self.assertEqual(_norm_hsn_class_name(""), "")
        self.assertEqual(_norm_hsn_class_name(None), "")


if __name__ == "__main__":
    unittest.main()
