"""Tests for the field-coverage report. Pure, runs locally:
    python -m unittest tally_migrator.tests.test_coverage
"""
import unittest
from unittest import mock

from tally_migrator.migration import coverage as cov
from tally_migrator.migration.coverage import (
    coverage_report, read_tags, value_kind, classify_field,
)


def _rich(filled, distinct, values, sample="", fill_rate=1.0, is_udf=False, count=None):
    """A raw_tags entry with the Step-2 distribution stats."""
    return {
        "count": count if count is not None else filled,
        "filled": filled, "fill_rate": fill_rate, "distinct": distinct,
        "values": values, "sample": sample or (values[0] if values else ""),
        "is_udf": is_udf, "records": ["Acme"],
    }


class _Src:
    """Stub source exposing only raw_tags(obj_type)."""
    def __init__(self, tags_by_type):
        self._t = tags_by_type

    def raw_tags(self, obj_type):
        return self._t.get(obj_type, {})


def _tag(count=1, sample="", records=None):
    return {"count": count, "sample": sample, "records": records or []}


class TestCoverage(unittest.TestCase):
    def test_clean_when_only_mapped_fields(self):
        # Every real Ledger tag the extractor reads → nothing unmapped.
        tags = {t: _tag() for t in read_tags("Ledger")}
        report = coverage_report(_Src({"Ledger": tags}))
        self.assertTrue(report["clean"])
        self.assertEqual(report["unmapped_field_count"], 0)
        self.assertEqual(report["types"], [])

    def test_internal_bank_config_tags_are_noise(self):
        # Tally-internal bank-config id + config effective-from date carry a real-
        # looking value but no business meaning; they must be hidden (counted as
        # noise), not listed as "won't migrate".
        tags = {
            "NAME": _tag(),
            "BANKINGCONFIGBANKID": _tag(11, "0", ["Acme"]),
            "STARTINGFROM": _tag(11, "20260401", ["Acme"]),
        }
        report = coverage_report(_Src({"Ledger": tags}))
        self.assertTrue(report["clean"])
        self.assertEqual(report["unmapped_field_count"], 0)
        self.assertEqual(report["noise_field_count"], 2)

    def test_internal_shapes_are_auto_detected_as_noise(self):
        # Generic shape heuristics catch internal tags without a curated entry:
        # GUID value, zeroed id pointer, effective-date marker, and empty-everywhere.
        tags = {
            "NAME": _tag(),
            "SOMEOBJGUID": _tag(11, "a1b2c3d4-1111-2222-3333", ["Acme"]),
            "WIDGETMASTERID": _tag(11, "0", ["Acme"]),
            "APPLICABLEFROM": _tag(11, "20240401", ["Acme"]),
            "BLANKUDF": _tag(11, "", ["Acme"]),
        }
        report = coverage_report(_Src({"Ledger": tags}))
        self.assertTrue(report["clean"])
        self.assertEqual(report["unmapped_field_count"], 0)
        self.assertEqual(report["noise_field_count"], 4)

    def test_real_idlike_udf_is_not_hidden(self):
        # A field ending in ID but carrying a real (non-zero) value might be a
        # meaningful external code - it must stay visible, not be auto-hidden.
        tags = {"NAME": _tag(), "LOYALTYCARDID": _tag(11, "884412", ["Acme"])}
        report = coverage_report(_Src({"Ledger": tags}))
        self.assertFalse(report["clean"])
        self.assertEqual(report["unmapped_field_count"], 1)

    def test_flags_udf_field(self):
        tags = {
            "NAME": _tag(),
            "GSTREGISTRATIONNUMBER": _tag(),          # mapped
            "CUSTOMERCATEGORY": _tag(3, "Wholesale", ["Acme", "Bolt Co"]),  # UDF
        }
        report = coverage_report(_Src({"Ledger": tags}))
        self.assertFalse(report["clean"])
        self.assertEqual(report["unmapped_field_count"], 1)
        led = report["types"][0]
        self.assertEqual(led["entity_type"], "Ledger")
        u = led["unmapped"][0]
        self.assertEqual(u["field"], "CUSTOMERCATEGORY")
        self.assertEqual(u["count"], 3)
        self.assertEqual(u["sample"], "Wholesale")
        self.assertEqual(u["examples"], ["Acme", "Bolt Co"])

    def test_ignores_housekeeping_tags(self):
        tags = {"NAME": _tag(), "GUID": _tag(), "MASTERID": _tag(), "ALTERID": _tag()}
        report = coverage_report(_Src({"Ledger": tags}))
        self.assertTrue(report["clean"])

    def test_multiple_entity_types(self):
        report = coverage_report(_Src({
            # Genuine UDFs carry a real value in at least one record (an empty-
            # everywhere field is noise, covered separately); PARENT is mapped.
            "Ledger": {"CUSTOMERCATEGORY": _tag(3, "Wholesale", ["Acme"])},
            "Stock Item": {"BATCHNAME": _tag(2, "B-204", ["Bolt"]), "PARENT": _tag()},
        }))
        self.assertEqual(report["unmapped_field_count"], 2)
        kinds = {t["entity_type"] for t in report["types"]}
        self.assertEqual(kinds, {"Ledger", "Stock Item"})

    def test_flags_read_but_not_written_field(self):
        # A field the extractor fetches (mapped) but no importer persists (not in
        # WRITTEN_FIELDS) must be reported as 'unwritten', not silently "clean".
        patched = {**cov.WRITTEN_FIELDS, "Ledger": ["Name", "Parent"]}
        with mock.patch.object(cov, "WRITTEN_FIELDS", patched):
            report = coverage_report(_Src({
                "Ledger": {"PARENT": _tag(), "OPENINGBALANCE": _tag(2, "5000 Dr", ["Cash"])},
            }))
        self.assertFalse(report["clean"])
        self.assertEqual(report["unmapped_field_count"], 0)
        self.assertEqual(report["unwritten_field_count"], 1)
        led = report["types"][0]
        self.assertEqual(led["unmapped"], [])
        self.assertEqual(led["unwritten"][0]["field"], "OPENINGBALANCE")
        self.assertEqual(led["unwritten"][0]["sample"], "5000 Dr")

    def test_real_tally_tags_are_not_flagged(self):
        # A genuine export uses LEDSTATENAME / EMAIL / ADDRESS.LIST instead of the
        # flat canonical tags - the parser reads them, so coverage must treat them
        # as covered, not "not migrated".
        tags = {
            "NAME": _tag(),
            "LEDSTATENAME": _tag(),
            "EMAIL": _tag(),
            "ADDRESS.LIST": _tag(),
        }
        report = coverage_report(_Src({"Ledger": tags}))
        self.assertTrue(report["clean"], report["types"])
        # And item price/cost revision-list containers on Stock Item.
        item_tags = {"NAME": _tag(), "STANDARDPRICELIST.LIST": _tag(),
                     "STANDARDCOSTLIST.LIST": _tag()}
        rep2 = coverage_report(_Src({"Stock Item": item_tags}))
        self.assertTrue(rep2["clean"], rep2["types"])

    def test_real_mapping_has_no_unwritten_gap(self):
        # Guard: with the real WRITTEN_FIELDS, every fetched item field is written
        # (regression guard for the GST-treatment silent-drop bug).
        tags = {t: _tag() for t in read_tags("Stock Item")}
        report = coverage_report(_Src({"Stock Item": tags}))
        self.assertEqual(report["unwritten_field_count"], 0)
        self.assertTrue(report["clean"])

    def test_noise_tags_suppressed_but_counted(self):
        # Tally internal flags / empty containers / audit / legacy-tax must not flood
        # the report, but the count is surfaced so nothing is hidden silently.
        tags = {
            "ISBILLWISEON": _tag(40, "No"),                 # boolean flag → noise
            "GSTDETAILS.LIST": _tag(40, ""),                # empty container → noise
            "UPDATEDDATETIME": _tag(40, "20260608"),        # audit → noise
            "VATDEALERTYPE": _tag(2, "Regular"),            # legacy tax prefix → noise
            "EXCISEDUTYTYPE": _tag(52, "Not Applicable"),   # legacy tax → noise
            "CUSTOMERCATEGORY": _tag(3, "Wholesale", ["Acme"]),  # real UDF → shown
        }
        report = coverage_report(_Src({"Ledger": tags}))
        self.assertEqual(report["unmapped_field_count"], 1)
        self.assertEqual(report["noise_field_count"], 5)
        self.assertEqual(report["types"][0]["unmapped"][0]["field"], "CUSTOMERCATEGORY")

    def test_real_value_field_is_not_noise(self):
        # A field carrying a genuine value (a bank number) is never treated as noise.
        tags = {"SOMEBANKFIELD": _tag(6, "61801504485", ["HDFC Bank"])}
        report = coverage_report(_Src({"Ledger": tags}))
        self.assertEqual(report["unmapped_field_count"], 1)
        self.assertEqual(report["noise_field_count"], 0)

    def test_no_target_tags_are_suppressed(self):
        # Bucket C: value-bearing tags with no ERPNext destination are hidden from
        # the per-field tables but still counted, so the report stays honest+short.
        report = coverage_report(_Src({
            "Group": {"GRPCREDITPARENT": _tag(40, ""), "GRPDEBITPARENT": _tag(40, "")},
            # LEDGERCOUNTRYISDCODE is a no-target tag (CURRENCYNAME is now a mapped
            # field - it drives the multi-currency party-opening guard).
            "Ledger": {"LEDGERCOUNTRYISDCODE": _tag(3, "91"), "TAXTYPE": _tag(52, "Others"),
                       "PRIORSTATENAME": _tag(3, "Maharashtra")},
            "Godown": {"ARE1SERIALMASTER": _tag(6, ""), "JOBNAME": _tag(6, ""),
                       "TAXUNITNAME": _tag(6, "")},
            "Stock Group": {"BASEUNITS": _tag(3, "Nos"), "COSTINGMETHOD": _tag(2, "Avg. Cost"),
                            "VALUATIONMETHOD": _tag(2, "Avg. Price")},
        }))
        self.assertTrue(report["clean"], report["types"])
        self.assertEqual(report["unmapped_field_count"], 0)
        self.assertEqual(report["noise_field_count"], 11)

    def test_valuation_and_gst_flat_tags_now_mapped(self):
        # Bucket A & B: valuation method + flat GST tags on a Stock Item are read
        # by the extractor, so they must count as covered (not "Not mapped").
        tags = {t: _tag() for t in read_tags("Stock Item")}
        tags.update({
            "VALUATIONMETHOD": _tag(1, "Avg. Price"),
            "COSTINGMETHOD": _tag(1, "Avg. Cost"),
            "GSTAPPLICABLE": _tag(1, "Applicable"),
            "GSTTYPEOFSUPPLY": _tag(1, "Goods"),
        })
        report = coverage_report(_Src({"Stock Item": tags}))
        self.assertTrue(report["clean"], report["types"])
        self.assertEqual(report["unwritten_field_count"], 0)


    # ── Derivation layer (labels / value-shape / redundancy) ─────────────────

    def test_value_kind_reads_the_value_shape(self):
        self.assertEqual(value_kind("27AABCR1234A1Z5"), "GST numbers")
        self.assertEqual(value_kind("AABCR1234A"), "PAN numbers")
        self.assertEqual(value_kind("sales@acme.in"), "email addresses")
        self.assertEqual(value_kind("20240401"), "dates")
        self.assertEqual(value_kind("+91 98200 12345"), "phone numbers")
        self.assertEqual(value_kind("15000.50"), "numbers")
        self.assertEqual(value_kind("Wholesale"), "")   # plain text → no shape
        self.assertEqual(value_kind(""), "")

    def test_rows_carry_raw_field_and_kind(self):
        tags = {"NAME": _tag(), "CUSTOMERCATEGORY": _tag(3, "Wholesale", ["Acme"])}
        report = coverage_report(_Src({"Ledger": tags}))
        row = report["types"][0]["unmapped"][0]
        self.assertEqual(row["field"], "CUSTOMERCATEGORY")   # raw Tally name, verbatim
        self.assertNotIn("label", row)                       # no humanized label
        self.assertEqual(row["kind"], "")

    # ── No-loss invariant (Step 1): every tag is accounted for ───────────────

    @staticmethod
    def _buckets_sum(report):
        return (report["imported_field_count"] + report["ignored_field_count"]
                + report["noise_field_count"] + report["redundant_field_count"]
                + report["unmapped_field_count"] + report["unwritten_field_count"])

    def test_every_tag_lands_in_exactly_one_bucket(self):
        # One tag of each of the six classes. The counts must sum to the total tag
        # count and ``accounted_for`` must hold - the no-loss guarantee, by number.
        patched = {**cov.WRITTEN_FIELDS, "Ledger": ["Name", "Parent"]}
        with mock.patch.object(cov, "WRITTEN_FIELDS", patched):
            report = coverage_report(_Src({"Ledger": {
                "GUID": _tag(),                                        # ignored
                "NAME": _tag(),                                        # ignored
                "PARENT": _tag(7, "Sundry Debtors", ["Acme"]),        # imported
                "OPENINGBALANCE": _tag(5, "5000 Dr", ["Acme"]),       # unwritten (loss)
                "GSTIN": _tag(8, "27AABCR1234A1Z5", ["Acme"]),        # redundant
                "ISBILLWISEON": _tag(40, "No"),                       # noise
                "CUSTOMERCATEGORY": _tag(3, "Wholesale", ["Acme"]),   # unmapped (loss)
            }}))
        self.assertEqual(report["total_tag_count"], 7)
        self.assertEqual(report["ignored_field_count"], 2)
        self.assertEqual(report["imported_field_count"], 1)
        self.assertEqual(report["unwritten_field_count"], 1)
        self.assertEqual(report["redundant_field_count"], 1)
        self.assertEqual(report["noise_field_count"], 1)
        self.assertEqual(report["unmapped_field_count"], 1)
        self.assertTrue(report["accounted_for"])
        self.assertEqual(self._buckets_sum(report), report["total_tag_count"])

    def test_noise_is_itemised_not_just_counted(self):
        # The previously-discarded noise rows are now retained in full on the type, so
        # the migration log can show every dropped field on demand (the audit trail).
        report = coverage_report(_Src({"Ledger": {
            "NAME": _tag(),
            "ISBILLWISEON": _tag(40, "No"),
            "CUSTOMERCATEGORY": _tag(3, "Wholesale", ["Acme"]),
        }}))
        led = report["types"][0]
        self.assertEqual([r["field"] for r in led["noise"]], ["ISBILLWISEON"])
        self.assertEqual(led["noise"][0]["sample"], "No")
        self.assertTrue(report["accounted_for"])

    def test_accounting_reconciles_on_real_field_set(self):
        # On the real mapped field set (plus a UDF, a redundant flat tag and noise),
        # the totals still reconcile - no field of a genuine export shape slips out.
        tags = {t: _tag(5, "x", ["Acme"]) for t in read_tags("Ledger")}
        tags.update({
            "GUID": _tag(), "MASTERID": _tag(),                       # ignored
            "GSTIN": _tag(8, "27AABCR1234A1Z5", ["Acme"]),           # redundant
            "ISBILLWISEON": _tag(40, "No"),                          # noise
            "LOYALTYTIER": _tag(3, "Gold", ["Acme"]),                # unmapped UDF
        })
        report = coverage_report(_Src({"Ledger": tags}))
        self.assertTrue(report["accounted_for"])
        self.assertEqual(self._buckets_sum(report), report["total_tag_count"])
        self.assertEqual(report["unmapped_field_count"], 1)

    def test_flat_tag_matching_a_nested_path_is_redundant_not_loss(self):
        # A flat <GSTIN> duplicates LEDGSTREGDETAILS.LIST/GSTIN, which we already
        # read - so it's redundant (not a loss) and the file stays clean. Detected
        # by leaf-name comparison, no hard-coded "GSTIN" rule.
        tags = {"NAME": _tag(), "GSTIN": _tag(8, "27AABCR1234A1Z5", ["Acme"])}
        report = coverage_report(_Src({"Ledger": tags}))
        self.assertTrue(report["clean"])
        self.assertEqual(report["unmapped_field_count"], 0)
        self.assertEqual(report["redundant_field_count"], 1)
        red = report["types"][0]["redundant"][0]
        self.assertEqual(red["field"], "GSTIN")
        self.assertEqual(red["kind"], "GST numbers")


class TestStep2Classification(unittest.TestCase):
    """Step 2: shape + importance derived from each unmapped field's distribution."""

    def test_boolean_shape(self):
        c = classify_field(_rich(10, 2, ["No", "Yes"]))
        self.assertEqual(c["shape"], "boolean")
        self.assertEqual(c["options"], ["No", "Yes"])

    def test_select_shape_low_cardinality(self):
        c = classify_field(_rich(20, 3, ["Distributor", "Retail", "Wholesale"]))
        self.assertEqual(c["shape"], "select")
        self.assertEqual(c["options"], ["Distributor", "Retail", "Wholesale"])

    def test_identifier_shape_high_cardinality(self):
        # distinct=None means "many" (cardinality cap exceeded) → free-form id.
        c = classify_field(_rich(48, None, []))
        self.assertEqual(c["shape"], "identifier")

    def test_typed_shape_from_value_kind(self):
        c = classify_field(_rich(40, None, [], sample="27AABCR1234A1Z5"))
        self.assertEqual(c["shape"], "typed")

    def test_constant_shape_single_value(self):
        c = classify_field(_rich(30, 1, ["Standard"]))
        self.assertEqual(c["shape"], "constant")

    def test_constant_zero_is_not_boolean(self):
        # Tally config scaffolding is a single "0" on every record - a constant, not
        # a boolean (even though "0" is in the boolean vocabulary), and it must score
        # ~0 so it never outranks a genuinely varying field on fill-rate alone.
        c = classify_field(_rich(50, 1, ["0"], fill_rate=1.0))
        self.assertEqual(c["shape"], "constant")
        self.assertEqual(c["score"], 0.0)
        varying = classify_field(_rich(5, 4, ["A", "B", "C", "D"], fill_rate=0.1))
        self.assertGreater(varying["score"], c["score"])

    def test_score_rises_with_fill_rate_and_udf(self):
        sparse = classify_field(_rich(1, 5, ["a", "b", "c", "d", "e"], fill_rate=0.02))
        full = classify_field(_rich(50, 5, ["a", "b", "c", "d", "e"], fill_rate=1.0))
        self.assertLess(sparse["score"], full["score"])
        plain = classify_field(_rich(50, 5, ["a", "b"], fill_rate=1.0))
        udf = classify_field(_rich(50, 5, ["a", "b"], fill_rate=1.0, is_udf=True))
        self.assertLess(plain["score"], udf["score"])

    def test_unmapped_rows_are_ranked_by_score(self):
        # A near-empty free-text UDF should sort BELOW a fully-populated typed one.
        report = coverage_report(_Src({"Ledger": {
            "NAME": _tag(),
            "RARELYUSEDNOTE": _rich(1, None, [], sample="misc", fill_rate=0.01),
            "PARTYGSTIN2": _rich(50, None, [], sample="27AABCR1234A1Z5", fill_rate=1.0),
        }}))
        rows = report["types"][0]["unmapped"]
        self.assertEqual([r["field"] for r in rows], ["PARTYGSTIN2", "RARELYUSEDNOTE"])
        self.assertEqual(rows[0]["shape"], "typed")

    def test_udf_yesno_escapes_noise_as_boolean(self):
        # A Yes/No field with the UDF namespace marker is a user-defined boolean, not
        # a built-in config flag - it must escape the Yes/No noise sentinel, land in
        # the actionable list, and classify as a Check candidate. A Yes/No field
        # WITHOUT the marker stays hidden as noise, exactly as before.
        report = coverage_report(_Src({"Ledger": {
            "NAME": _tag(),
            "ISVIPCUSTOMER": _rich(10, 2, ["No", "Yes"], is_udf=True),   # business UDF
            "ISBILLWISEON": _rich(10, 2, ["No", "Yes"], is_udf=False),   # config flag
        }}))
        led = report["types"][0]
        unmapped = {r["field"]: r for r in led["unmapped"]}
        self.assertIn("ISVIPCUSTOMER", unmapped)
        self.assertEqual(unmapped["ISVIPCUSTOMER"]["shape"], "boolean")
        self.assertEqual(report["unmapped_field_count"], 1)
        noise = {r["field"] for r in led["noise"]}
        self.assertIn("ISBILLWISEON", noise)

    def test_classification_degrades_without_distribution_stats(self):
        # Legacy {count, sample} entry (no distinct/fill_rate) still classifies.
        c = classify_field({"count": 3, "sample": "Wholesale", "records": ["Acme"]})
        self.assertEqual(c["shape"], "freetext")
        self.assertEqual(c["fill_rate"], 0.0)


if __name__ == "__main__":
    unittest.main()
