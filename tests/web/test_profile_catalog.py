#!/usr/bin/env python3

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import (  # noqa: E402
    CONDITIONAL_EXPRESSION,
    CONDITIONAL_PROCESS_ID,
    FILAMENT_ID,
    MACHINE_ID,
    OTHER_MACHINE_ID,
    OTHER_PROCESS_ID,
    PROCESS_ID,
    VENDOR,
    write_profile_tree,
)
from web_profile_catalog import ProfileCatalogError, load_catalog  # noqa: E402


class ProfileCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        write_profile_tree(self.root)
        self.catalog = load_catalog(self.root, [VENDOR])

    def tearDown(self):
        self.temporary.cleanup()

    def test_lists_only_user_selectable_profiles(self):
        machines = [entry.name for entry in self.catalog.list("machine")]
        self.assertEqual(machines, ["Other Printer 0.4 nozzle", "Test Printer 0.4 nozzle"])
        self.assertEqual(len(self.catalog), 6)

    def test_ignores_a_vendor_entry_that_escapes_its_directory(self):
        self.assertTrue(all("passwd" not in entry.profile_id for entry in self.catalog.list("machine")))

    def test_describes_a_printer_with_the_fields_the_screen_needs(self):
        described = self.catalog.get(MACHINE_ID, "machine").describe()
        self.assertEqual(described["printer_model"], "Test Printer")
        self.assertEqual(described["nozzle_diameter"], "0.4")
        self.assertEqual(described["default_process"], "0.20mm Standard @Test")

    def test_filters_process_and_filament_by_the_selected_printer(self):
        printer = self.catalog.get(MACHINE_ID, "machine")
        # An unresolved condition suits every printer, exactly as the desktop
        # falls back when it cannot answer one.
        self.assertEqual(
            [entry.profile_id for entry in self.catalog.list("process", printer)],
            [CONDITIONAL_PROCESS_ID, PROCESS_ID],
        )
        # A filament that declares no compatible printers suits every printer.
        self.assertEqual([entry.profile_id for entry in self.catalog.list("filament", printer)], [FILAMENT_ID])
        other = self.catalog.get(OTHER_MACHINE_ID, "machine")
        self.assertEqual(
            [entry.profile_id for entry in self.catalog.list("process", other)],
            [CONDITIONAL_PROCESS_ID, OTHER_PROCESS_ID],
        )

    def test_resolves_conditions_through_the_engine(self):
        seen = {}

        def evaluate(request):
            query = json.loads(request.read_text(encoding="utf-8"))
            seen.update(query)
            # Only the profile that declares an expression is ever asked about.
            return {
                "compatibility": {
                    candidate["id"]: [
                        printer["id"]
                        for printer in query["printers"]
                        if candidate["condition"] in printer["name"]
                    ]
                    for candidate in query["candidates"]
                }
            }

        self.assertEqual(self.catalog.resolve_conditions(evaluate), 1)
        self.assertEqual([candidate["id"] for candidate in seen["candidates"]], [CONDITIONAL_PROCESS_ID])
        self.assertEqual(seen["candidates"][0]["condition"], CONDITIONAL_EXPRESSION)

        printer = self.catalog.get(MACHINE_ID, "machine")
        self.assertEqual(
            [entry.profile_id for entry in self.catalog.list("process", printer)],
            [CONDITIONAL_PROCESS_ID, PROCESS_ID],
        )
        other = self.catalog.get(OTHER_MACHINE_ID, "machine")
        self.assertEqual([entry.profile_id for entry in self.catalog.list("process", other)], [OTHER_PROCESS_ID])

    def test_reports_the_flattened_inheritance_chain(self):
        described = self.catalog.get(PROCESS_ID, "process").describe()
        self.assertEqual(described["inherits_chain"], ["fdm_process_common", "0.20mm Standard @Test"])
        # A profile with no parent is a chain of one.
        self.assertEqual(
            self.catalog.get(FILAMENT_ID, "filament").describe()["inherits_chain"],
            ["fdm_filament_pla", "Test Generic PLA"],
        )

    def test_rejects_an_unknown_profile_and_a_mismatched_kind(self):
        with self.assertRaises(ProfileCatalogError) as raised:
            self.catalog.get("Testing/machine/Nonexistent", "machine")
        self.assertEqual(raised.exception.code, "unknown_profile")
        with self.assertRaises(ProfileCatalogError) as raised:
            self.catalog.get(MACHINE_ID, "process")
        self.assertEqual(raised.exception.code, "profile_kind_mismatch")

    def test_materializes_a_flattened_chain(self):
        destination = self.root / "job" / "profiles"
        written = self.catalog.materialize(MACHINE_ID, PROCESS_ID, FILAMENT_ID, destination)
        self.assertEqual(sorted(written), ["filament", "machine", "process"])
        process = json.loads(written["process"].read_text(encoding="utf-8"))
        # Inherited defaults are merged in and the inheritance link is gone, so
        # the worker never reads the bundled profile tree.
        self.assertEqual(process["sparse_infill_density"], "15%")
        self.assertNotIn("inherits", process)
        self.assertEqual(json.loads(written["machine"].read_text(encoding="utf-8"))["printable_height"], "250")
        # The filament inherits across directories, not from a sibling file.
        self.assertEqual(json.loads(written["filament"].read_text(encoding="utf-8"))["filament_type"], ["PLA"])

    def test_refuses_to_materialize_an_incompatible_chain(self):
        with self.assertRaises(ProfileCatalogError) as raised:
            self.catalog.materialize(MACHINE_ID, OTHER_PROCESS_ID, FILAMENT_ID, self.root / "job")
        self.assertEqual(raised.exception.code, "incompatible_profile")

    def test_rejects_an_unknown_or_unsafe_vendor(self):
        for vendor, code in (("Nonexistent", "unknown_profile_vendor"), ("../secrets", "invalid_profile_vendor")):
            with self.assertRaises(ProfileCatalogError) as raised:
                load_catalog(self.root, [vendor])
            self.assertEqual(raised.exception.code, code)


class BundledProfileTests(unittest.TestCase):
    """The default vendor must stay loadable, because the API serves it."""

    def test_the_default_vendor_offers_the_baseline_chain(self):
        catalog = load_catalog(REPO_ROOT)
        machine = catalog.get("Anycubic/machine/Anycubic Kobra 0.4 nozzle", "machine")
        compatible = [entry.profile_id for entry in catalog.list("process", machine)]
        self.assertIn("Anycubic/process/0.20mm Standard @Anycubic Kobra", compatible)
        filaments = [entry.profile_id for entry in catalog.list("filament", machine)]
        self.assertIn("Anycubic/filament/Anycubic Generic PLA", filaments)
        self.assertTrue(all(entry.kind == "machine" for entry in catalog.list("machine")))


if __name__ == "__main__":
    unittest.main()
