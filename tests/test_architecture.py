from __future__ import annotations

import copy
import datetime
import hashlib
import os
import tempfile
import unittest
from unittest.mock import patch

from openpyxl import Workbook
import yaml

from src.engine.client_map import (
    MappedStatement,
    Period,
    Taxonomy,
    canonicalize_xlsx,
    map_statement,
    propose_unmapped_mappings,
    validate_template_taxonomy,
    verify_mapped,
)
from src.engine.sector_config import (
    available_sector_ids,
    load_sector_assets,
    load_sector_config,
)


class SectorConfigurationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config, cls.template, cls.taxonomy = load_sector_assets("software")

    def test_sector_is_discovered_without_a_code_registry(self):
        self.assertIn("software", available_sector_ids())
        self.assertEqual("software", load_sector_config("software").sector_id)

    def test_template_and_taxonomy_have_exact_field_coverage(self):
        validate_template_taxonomy(self.template, self.taxonomy)
        template_pairs = {
            (statement, field.fid)
            for (statement, _scope), fields in self.template.items()
            for field in fields
        }
        taxonomy_pairs = {
            (statement, item["fid"])
            for statement, items in self.taxonomy.items()
            for item in items
        }
        self.assertEqual(template_pairs, taxonomy_pairs)

    def test_taxonomy_is_the_only_runtime_field_source(self):
        self.assertFalse(hasattr(self.config, "template_path"))
        with open(self.config.taxonomy_path, encoding="utf-8") as fh:
            document = yaml.safe_load(fh)
        self.assertEqual({
            "ambiguity": "reject",
            "sign": "preserve_source",
            "unit_and_time_nature": "strict",
            "total_component_boundary": "exact",
            "model_authority": "proposal_only",
        }, document["mapping_policy"])
        self.assertEqual(410, document["expected_unique_field_count"])
        self.assertEqual(795, document["expected_scope_assignment_count"])
        self.assertNotIn("schema_version", document)
        self.assertEqual(
            {"income", "balance", "cashflow", "segment"},
            set(document["statement_sections"]),
        )
        self.assertTrue(document["location_vocabulary"])
        self.assertTrue(document["identities"])
        self.assertTrue(all(
            "fid" in term
            for checks in document["identities"].values()
            for check in checks
            for term in (
                check["lhs"] + check.get("alternate_lhs", [])
                + check.get("optional_lhs", [])
            )
        ))
        calculations = [
            scoped["calculation"]
            for item in document["items"]
            for scoped in item["scopes"].values()
        ]
        self.assertTrue(any(calc["type"] == "sum" for calc in calculations))
        self.assertTrue(any(calc["type"] == "ratio" for calc in calculations))
        self.assertTrue(all(
            "C" not in str(calc) for calc in calculations
        ), "formulas must reference FIDs, never Excel row addresses")
        self.assertTrue(all(item.get("unit") for item in document["items"]))
        self.assertTrue(all(
            item.get("time_nature") for item in document["items"]))
        self.assertTrue(all(
            item["mapping"]["locations"]
            and "match_name" in item["mapping"]
            and item["mapping"]["location_source"] == "declared"
            and item["mapping"]["mode"] in {
                "canonical_name_and_aliases", "aliases_only", "rules_only",
                "disabled",
            }
            for item in document["items"]))
        required_definition_keys = {
            "meaning", "includes", "excludes", "mapping_notes",
            "distinguish_from",
        }
        self.assertTrue(all(
            required_definition_keys <= set(item["definition"])
            for item in document["items"]
        ))
        self.assertTrue(all(
            isinstance(item["definition"]["includes"], list)
            and isinstance(item["definition"]["excludes"], list)
            and isinstance(item["definition"]["mapping_notes"], list)
            for item in document["items"]))
        by_statement_fid = {
            (item["statement"], str(item["fid"])): item
            for item in document["items"]
        }
        for item in document["items"]:
            for competitor in item["definition"]["distinguish_from"]:
                other = by_statement_fid[
                    (item["statement"], str(competitor))]
                self.assertIn(
                    str(item["fid"]),
                    {str(value)
                     for value in other["definition"]["distinguish_from"]},
                )

    def test_missing_taxonomy_field_fails_fast(self):
        broken = copy.deepcopy(self.taxonomy)
        broken["income"] = [
            item for item in broken["income"] if item["fid"] != "274"
        ]
        with self.assertRaisesRegex(ValueError, r"missing from taxonomy: 274"):
            validate_template_taxonomy(self.template, broken)

    def test_unsafe_active_alias_collision_fails_fast(self):
        broken = copy.deepcopy(self.taxonomy)
        revenue = next(
            item for item in broken["income"] if item["fid"] == "256")
        other_income = next(
            item for item in broken["income"] if item["fid"] == "265")
        other_income["aliases"].append(revenue["name"])
        with self.assertRaisesRegex(
                ValueError, r"ambiguous active taxonomy aliases"):
            validate_template_taxonomy(self.template, broken)

    def test_identity_verifier_interprets_taxonomy_without_known_fids(self):
        taxonomy = Taxonomy(identities={
            "income": [{
                "name": "declarative test identity",
                "lhs": [
                    {"fid": "left-a", "sign": 1},
                    {"fid": "left-b", "sign": 1},
                ],
                "rhs": "right",
            }],
        })
        mapped = MappedStatement(
            periods=[Period("FY", "2026-03-31", "", 1)],
            facts={
                "left-a": {1: 4.0},
                "left-b": {1: 5.0},
                "right": {1: 11.0},
            },
            sources={},
            unmapped=[],
            verification=[],
        )
        verify_mapped(mapped, "income", taxonomy)
        self.assertTrue(mapped.flags)
        self.assertIn("declarative test identity", mapped.flags[0])

    def test_unknown_sector_is_not_fallback_hardcoded(self):
        with self.assertRaises(FileNotFoundError):
            load_sector_config("not_configured")

    def test_unambiguous_exact_label_does_not_call_the_model(self):
        grid = [
            ["Particulars", "Quarter ended March 31, 2026"],
            ["Revenue from operations", "100.00"],
        ]
        with patch("src.llm.extract_json",
                   side_effect=AssertionError("model should not be called")):
            mapped = map_statement(
                grid,
                "income",
                self.taxonomy,
                self.template[("income", "standalone")],
            )
        self.assertEqual({1: 100.0}, mapped.facts["256"])

    def test_unresolved_label_is_not_promoted_by_the_model(self):
        grid = [
            ["Particulars", "Quarter ended March 31, 2026"],
            ["A filing-specific mystery expense", "12.00"],
        ]
        with patch("src.llm.extract_json",
                   side_effect=AssertionError("authoritative mapper called model")):
            mapped = map_statement(
                grid,
                "income",
                self.taxonomy,
                self.template[("income", "standalone")],
                scope="standalone",
            )
        self.assertEqual({}, mapped.facts)
        self.assertEqual(
            ["A filing-specific mystery expense"], mapped.unmapped)

        authoritative_before = copy.deepcopy(mapped.facts)
        with patch("src.llm.extract_json", return_value={
            "assignments": [{"line": 1, "fid": "279"}],
        }):
            payload = propose_unmapped_mappings(
                {("income", "standalone"): mapped},
                self.taxonomy,
                self.template,
            )
        self.assertEqual(authoritative_before, mapped.facts)
        self.assertFalse(payload["authoritative_report_affected"])
        self.assertEqual("proposal_only", payload["authority"])
        self.assertEqual("279", payload["proposals"][0]["suggested_fid"])
        self.assertEqual("unreviewed", payload["proposals"][0]["status"])

    def test_same_cashflow_label_is_resolved_by_declared_location(self):
        grid = [
            ["Particulars", "Year ended March 31, 2026"],
            ["Cash flow from operating activities", ""],
            ["Other adjustments", "169"],
            ["Net cash generated by operating activities", "28164"],
        ]
        with patch("src.llm.extract_json",
                   side_effect=AssertionError("model should not be called")):
            mapped = map_statement(
                grid,
                "cashflow",
                self.taxonomy,
                self.template[("cashflow", "standalone")],
            )
        self.assertEqual({1: 169.0}, mapped.facts["17542"])
        self.assertEqual({1: 28164.0}, mapped.facts["17538"])
        self.assertNotIn("30513", mapped.facts)

    def test_balance_cash_does_not_change_current_asset_location(self):
        grid = [
            ["Particulars", "As at March 31, 2026"],
            ["ASSETS", ""],
            ["Non-current assets", ""],
            ["Loans", "10"],
            ["Current assets", ""],
            ["Cash and cash equivalents", "20"],
            ["Loans", "30"],
            ["Other financial assets", "40"],
        ]
        mapped = map_statement(
            grid,
            "balance",
            self.taxonomy,
            self.template[("balance", "standalone")],
            scope="standalone",
        )
        self.assertEqual({1: 10.0}, mapped.facts["20217"])
        self.assertEqual({1: 20.0}, mapped.facts["13722"])
        self.assertEqual({1: 30.0}, mapped.facts["20230"])
        self.assertEqual({1: 40.0}, mapped.facts["20231"])

    def test_missing_initial_noncurrent_heading_is_recovered_from_boundary(self):
        grid = [
            ["Particulars", "As at March 31, 2026"],
            ["Property, plant and equipment", "100"],
            ["Goodwill", "20"],
            ["Investments", "30"],
            ["Current assets", ""],
            ["Investments", "40"],
            ["Cash and cash equivalents", "50"],
        ]
        mapped = map_statement(
            grid,
            "balance",
            self.taxonomy,
            self.template[("balance", "standalone")],
            scope="standalone",
        )
        self.assertEqual({1: 100.0}, mapped.facts["20222"])
        self.assertEqual({1: 20.0}, mapped.facts["20220"])
        self.assertEqual({1: 30.0}, mapped.facts["13748"])
        self.assertEqual({1: 40.0}, mapped.facts["13724"])
        self.assertEqual({1: 50.0}, mapped.facts["13722"])


class WorkbookDeterminismTests(unittest.TestCase):
    def test_xlsx_metadata_is_byte_deterministic(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = [os.path.join(directory, f"book-{index}.xlsx")
                     for index in (1, 2)]
            for index, path in enumerate(paths):
                workbook = Workbook()
                workbook.active["A1"] = "same financial data"
                workbook.properties.created = datetime.datetime(
                    2020 + index, 1, 1)
                workbook.properties.modified = datetime.datetime(
                    2020 + index, 1, 2)
                workbook.save(path)
                canonicalize_xlsx(path)
            digests = []
            for path in paths:
                with open(path, "rb") as workbook_file:
                    digests.append(
                        hashlib.sha256(workbook_file.read()).hexdigest())
            self.assertEqual(digests[0], digests[1])

    def test_verified_report_is_restored_byte_for_byte_by_content_key(self):
        import src.webapp as webapp

        with tempfile.TemporaryDirectory() as directory:
            client_dir = os.path.join(directory, "client")
            raw_dir = os.path.join(directory, "raw")
            canonical_dir = os.path.join(directory, "canonical")
            with (
                patch.object(webapp, "CLIENT_DIR", client_dir),
                patch.object(webapp, "QTR_RAW_DIR", raw_dir),
                patch.object(webapp, "CANONICAL_REPORT_DIR", canonical_dir),
            ):
                name, raw_name = "TEST_Q4FY2026", "test_q4FY2026"
                fingerprint = {"pdf_sha256": "abc", "pipeline_sha256": "def"}
                artifacts = webapp._report_artifacts(name, raw_name)
                original = {
                    "workbook": b"workbook-bytes",
                    "mapped": b"mapped-bytes",
                    "raw": b"raw-bytes",
                }
                for kind, path in artifacts.items():
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    with open(path, "wb") as artifact:
                        artifact.write(original[kind])
                self.assertIsNotNone(webapp._publish_canonical_report(
                    name, raw_name, fingerprint))
                for path in artifacts.values():
                    os.remove(path)
                self.assertTrue(webapp._restore_canonical_report(
                    name, raw_name, fingerprint))
                for kind, path in artifacts.items():
                    with open(path, "rb") as artifact:
                        self.assertEqual(original[kind], artifact.read())


if __name__ == "__main__":
    unittest.main()
