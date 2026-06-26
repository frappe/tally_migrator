"""Phase 1 guard: the streaming bytes parse must be byte-identical to the legacy
whole-document (str) parse, including across chunk boundaries, and must keep the
DTD/entity safety. No site needed - pure parsing."""
import io
import unittest
from unittest import mock

from tally_migrator.tally import file_source
from tally_migrator.tally.file_source import FileTallySource

# A document with the real Tally nesting (records four levels deep under
# REQUESTDATA>TALLYMESSAGE), two record types, a nested .LIST, a numeric char
# reference Tally emits (&#4;), and a stray illegal control char (\x05).
DOC = (
    "<ENVELOPE><BODY><IMPORTDATA><REQUESTDATA>"
    "<TALLYMESSAGE><LEDGER NAME='Al&#4;pha'><PARENT>Sundry Debtors</PARENT>"
    "<ADDRESS.LIST><ADDRESS>L1\x05</ADDRESS><ADDRESS>L2</ADDRESS></ADDRESS.LIST></LEDGER></TALLYMESSAGE>"
    "<TALLYMESSAGE><LEDGER NAME='Beta'><PARENT>Sundry Creditors</PARENT></LEDGER></TALLYMESSAGE>"
    "<TALLYMESSAGE><STOCKITEM NAME='Widget'><PARENT>Goods</PARENT></STOCKITEM></TALLYMESSAGE>"
    "</REQUESTDATA></IMPORTDATA></BODY></ENVELOPE>"
)


def collections(src):
    # _name is the record's NAME attribute (the real key the extractor uses); the
    # requested fields resolve to child tags. This mirrors actual consumption.
    return {
        "ledgers": src.get_collection("Ledger", ["PARENT"]),
        "items": src.get_collection("Stock Item", ["PARENT"]),
        "addr": src.get_child_list("Ledger", "ADDRESS.LIST", ["ADDRESS"]),
    }


class TestStreamingEqualsLegacy(unittest.TestCase):
    def _utf16(self, text):
        return ("﻿" + text).encode("utf-16-le")   # BOM + LE, a real Tally shape

    def test_bytes_stream_equals_str_path(self):
        legacy = collections(FileTallySource(DOC))             # str -> whole-document path
        streamed = collections(FileTallySource(self._utf16(DOC)))   # bytes -> streaming path
        self.assertEqual(streamed, legacy)
        # sanity: the illegal char ref (&#4;) was stripped from the NAME so the record
        # keys on the cleaned value, exactly as the whole-document path produces.
        self.assertEqual(legacy["ledgers"][0]["_name"], "Alpha")
        self.assertEqual([l["_name"] for l in legacy["ledgers"]], ["Alpha", "Beta"])

    def test_identical_across_tiny_chunks(self):
        # Force 4-byte reads so records, the char ref and tags all span boundaries.
        with mock.patch.object(file_source._SanitizingReader, "_CHUNK", 4):
            streamed = collections(FileTallySource(self._utf16(DOC)))
        self.assertEqual(streamed, collections(FileTallySource(DOC)))

    def test_streaming_rejects_dtd(self):
        evil = "<!DOCTYPE x [<!ENTITY a 'b'>]>" + DOC
        with self.assertRaises(Exception):
            FileTallySource(("﻿" + evil).encode("utf-16-le"))

    def test_streaming_rejects_dtd_split_across_chunk(self):
        evil = "<!DOCTYPE foo>" + DOC
        with mock.patch.object(file_source._SanitizingReader, "_CHUNK", 4):
            with self.assertRaises(Exception):
                FileTallySource(("﻿" + evil).encode("utf-16-le"))

    def test_utf8_bom_bytes_stream(self):
        src = FileTallySource(("﻿" + DOC).encode("utf-8"))
        self.assertEqual(collections(src), collections(FileTallySource(DOC)))

    def test_str_path_unchanged_for_plain_text(self):
        # A bare str still parses (legacy callers/tests) and yields the records.
        src = FileTallySource(DOC)
        self.assertEqual([l["_name"] for l in src.get_collection("Ledger", ["PARENT"])],
                         ["Alpha", "Beta"])


if __name__ == "__main__":
    unittest.main()
