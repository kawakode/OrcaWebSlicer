#!/usr/bin/env python3

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import FAKE_WORKER  # noqa: E402
from web_settings_catalog import (  # noqa: E402
    SETTINGS_CATALOG_VERSION,
    SettingsCatalogError,
    evaluate_compatibility,
    load_settings_catalog,
    parse_settings_catalog,
)


def catalog(**overrides):
    document = {
        "catalog_version": SETTINGS_CATALOG_VERSION,
        "engine_version": "0.0.0-test",
        "groups": [{"id": "quality", "label": "Quality"}],
        "settings": [
            {
                "key": "layer_height",
                "group": "quality",
                "scope": "process",
                "type": "float",
                "vector": False,
                "label": "Layer height",
                "unit": "mm",
                "min": 0.01,
                "max": 0.6,
                "default": "0.2",
            }
        ],
    }
    document.update(overrides)
    return json.dumps(document)


class ParseTests(unittest.TestCase):
    def test_rejects_a_catalog_this_api_cannot_speak(self):
        for serialized, code in (
            ("not json", "unreadable_settings_catalog"),
            (catalog(catalog_version=99), "unsupported_settings_catalog_version"),
            (catalog(settings=[]), "unreadable_settings_catalog"),
        ):
            with self.subTest(code=code):
                with self.assertRaises(SettingsCatalogError) as raised:
                    parse_settings_catalog(serialized)
                self.assertEqual(raised.exception.code, code)

    def test_rejects_a_setting_in_an_undeclared_group(self):
        orphaned = json.loads(catalog())
        orphaned["settings"][0]["group"] = "nowhere"
        with self.assertRaises(SettingsCatalogError) as raised:
            parse_settings_catalog(json.dumps(orphaned))
        self.assertEqual(raised.exception.code, "unreadable_settings_catalog")

    def test_rejects_a_duplicated_key(self):
        duplicated = json.loads(catalog())
        duplicated["settings"].append(dict(duplicated["settings"][0]))
        with self.assertRaises(SettingsCatalogError) as raised:
            parse_settings_catalog(json.dumps(duplicated))
        self.assertEqual(raised.exception.code, "unreadable_settings_catalog")


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.catalog = parse_settings_catalog(catalog())

    def test_accepts_a_value_inside_the_engine_range(self):
        self.catalog.validate_overrides({"layer_height": "0.28"})

    def test_names_the_setting_and_the_bound_it_broke(self):
        for value, message in (("0.9", "at most 0.6mm"), ("0.001", "at least 0.01mm"), ("thick", "expected a number")):
            with self.subTest(value=value):
                with self.assertRaises(SettingsCatalogError) as raised:
                    self.catalog.validate_overrides({"layer_height": value})
                self.assertEqual(raised.exception.code, "invalid_setting_value")
                self.assertIn(message, str(raised.exception))

    def test_refuses_a_setting_this_deployment_does_not_expose(self):
        with self.assertRaises(SettingsCatalogError) as raised:
            self.catalog.validate_overrides({"wall_loops": "2"})
        self.assertEqual(raised.exception.code, "unknown_setting")

    def test_holds_every_element_of_a_vector_to_the_scalar_rule(self):
        vectors = parse_settings_catalog(
            catalog(
                settings=[
                    {"key": "nozzle_temperature", "group": "quality", "type": "ints", "vector": True,
                     "min": 0, "max": 500, "unit": "℃"}
                ]
            )
        )
        vectors.validate_overrides({"nozzle_temperature": "220,215"})
        with self.assertRaises(SettingsCatalogError):
            vectors.validate_overrides({"nozzle_temperature": "220,900"})
        with self.assertRaises(SettingsCatalogError):
            vectors.validate_overrides({"nozzle_temperature": "220,"})

    def test_accepts_a_percentage_only_where_the_engine_does(self):
        percents = parse_settings_catalog(
            catalog(
                settings=[
                    {"key": "sparse_infill_density", "group": "quality", "type": "percent", "vector": False,
                     "min": 0, "max": 100},
                    {"key": "outer_wall_speed", "group": "quality", "type": "float_or_percent", "vector": False,
                     "min": 0, "max": 500, "ratio_over": "inner_wall_speed"},
                ]
            )
        )
        percents.validate_overrides({"sparse_infill_density": "15%"})
        # A ratio expressed as a percentage is bounded by what it is a ratio
        # over, so this setting's own absolute range does not apply.
        percents.validate_overrides({"outer_wall_speed": "600%"})
        with self.assertRaises(SettingsCatalogError):
            percents.validate_overrides({"outer_wall_speed": "600"})


class WorkerTests(unittest.TestCase):
    """The transport: the worker command, its exit code, and its output."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.worker = self.root / "fake_worker.py"
        self.worker.write_text(FAKE_WORKER, encoding="utf-8")

    def tearDown(self):
        self.temporary.cleanup()

    def command(self, mode="success"):
        return (sys.executable, str(self.worker), mode)

    def test_reads_the_catalog_the_worker_exports(self):
        loaded = load_settings_catalog(self.command())
        self.assertEqual(loaded.catalog_version, SETTINGS_CATALOG_VERSION)
        self.assertEqual(loaded.definition("layer_height")["unit"], "mm")
        self.assertIsNone(loaded.definition("nonexistent"))

    def test_reports_a_worker_that_cannot_answer(self):
        for command, code in (
            (self.command("no-metadata"), "settings_catalog_unavailable"),
            ((str(self.root / "missing"),), "settings_catalog_unavailable"),
        ):
            with self.subTest(code=code):
                with self.assertRaises(SettingsCatalogError) as raised:
                    load_settings_catalog(command)
                self.assertEqual(raised.exception.code, code)

    def test_reads_the_compatibility_answer(self):
        request = self.root / "compatibility.json"
        (self.root / "printers").mkdir()
        (self.root / "printers" / "0.json").write_text("{}", encoding="utf-8")
        request.write_text(
            json.dumps(
                {
                    "catalog_version": 1,
                    "printers": [{"id": "p", "name": "Test Printer", "profile": "printers/0.json"}],
                    "candidates": [{"id": "c", "condition": "Test"}],
                }
            ),
            encoding="utf-8",
        )
        answered = evaluate_compatibility(self.command(), str(request))
        self.assertEqual(answered["compatibility"], {"c": ["p"]})


if __name__ == "__main__":
    unittest.main()
