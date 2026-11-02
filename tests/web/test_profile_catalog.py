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
    CONDITIONAL_PROCESS_ID,
    DUAL_MACHINE_ID,
    FILAMENT_ID,
    MACHINE_ID,
    OTHER_MACHINE_ID,
    OTHER_PROCESS_ID,
    PETG_ID,
    PROCESS_ID,
    VENDOR,
    WIDE_NOZZLE_FILAMENT_ID,
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
        self.assertEqual(machines, ["Dual Printer", "Other Printer 0.4 nozzle", "Test Printer 0.4 nozzle"])
        self.assertEqual(len(self.catalog), 9)

    def test_ignores_a_vendor_entry_that_escapes_its_directory(self):
        self.assertTrue(all("passwd" not in entry.profile_id for entry in self.catalog.list("machine")))

    def test_describes_a_printer_with_the_fields_the_screen_needs(self):
        described = self.catalog.get(MACHINE_ID, "machine").describe()
        self.assertEqual(described["printer_model"], "Test Printer")
        self.assertEqual(described["nozzle_diameter"], "0.4")
        self.assertEqual(described["default_process"], "0.20mm Standard @Test")

    def test_filters_process_and_filament_by_the_selected_printer(self):
        printer = self.catalog.get(MACHINE_ID, "machine")
        # Without an evaluator a conditional profile is offered everywhere,
        # which is the desktop's answer for a condition it cannot read.
        self.assertEqual(
            [entry.profile_id for entry in self.catalog.list("process", printer)],
            [PROCESS_ID, CONDITIONAL_PROCESS_ID],
        )
        # A filament that declares no compatible printers suits every printer.
        self.assertEqual(
            [entry.profile_id for entry in self.catalog.list("filament", printer)],
            [WIDE_NOZZLE_FILAMENT_ID, PETG_ID, FILAMENT_ID],
        )
        other = self.catalog.get(OTHER_MACHINE_ID, "machine")
        self.assertEqual(
            [entry.profile_id for entry in self.catalog.list("process", other)],
            [OTHER_PROCESS_ID, CONDITIONAL_PROCESS_ID],
        )

    def test_narrows_a_conditional_profile_through_the_evaluator(self):
        seen = {}

        def evaluator(request_path):
            request = json.loads(request_path.read_text(encoding="utf-8"))
            seen.update(request)
            # Every printer is flattened beside the request, never read from
            # the bundled tree.
            for printer in request["printers"]:
                staged = json.loads((request_path.parent / printer["profile"]).read_text(encoding="utf-8"))
                self.assertNotIn("inherits", staged)
            return {"compatibility": {CONDITIONAL_PROCESS_ID: [OTHER_MACHINE_ID]}}

        catalog = load_catalog(self.root, [VENDOR], evaluator)
        self.assertEqual(
            [candidate["id"] for candidate in seen["candidates"]], [CONDITIONAL_PROCESS_ID, WIDE_NOZZLE_FILAMENT_ID]
        )
        printer = catalog.get(MACHINE_ID, "machine")
        self.assertEqual([entry.profile_id for entry in catalog.list("process", printer)], [PROCESS_ID])
        other = catalog.get(OTHER_MACHINE_ID, "machine")
        self.assertIn(CONDITIONAL_PROCESS_ID, [entry.profile_id for entry in catalog.list("process", other)])
        with self.assertRaises(ProfileCatalogError) as raised:
            catalog.materialize(MACHINE_ID, CONDITIONAL_PROCESS_ID, FILAMENT_ID, self.root / "job")
        self.assertEqual(raised.exception.code, "incompatible_profile")

    def test_judges_a_filament_on_each_extruder_of_a_multi_nozzle_printer(self):
        staged = {}

        def evaluator(request_path):
            request = json.loads(request_path.read_text(encoding="utf-8"))
            for printer in request["printers"]:
                profile = json.loads((request_path.parent / printer["profile"]).read_text(encoding="utf-8"))
                staged[printer["id"]] = profile["nozzle_diameter"]
            return {
                "compatibility": {
                    WIDE_NOZZLE_FILAMENT_ID: [
                        identifier for identifier, nozzles in staged.items() if nozzles[0] == "0.6"
                    ]
                }
            }

        catalog = load_catalog(self.root, [VENDOR], evaluator)
        # The second extruder is evaluated with its own nozzle in front.
        self.assertEqual(staged[DUAL_MACHINE_ID], ["0.4", "0.6"])
        self.assertEqual(staged[DUAL_MACHINE_ID + "#extruder-2"], ["0.6", "0.4"])
        dual = catalog.get(DUAL_MACHINE_ID, "machine")
        self.assertEqual(dual.describe()["extruder_count"], 2)
        self.assertEqual(dual.describe()["max_filaments"], 2)
        wide = catalog.get(WIDE_NOZZLE_FILAMENT_ID, "filament")
        self.assertEqual(catalog.compatible_extruders(wide, dual), [2])
        self.assertIn(wide, catalog.list("filament", dual))
        self.assertNotIn(wide, catalog.list("filament", catalog.get(MACHINE_ID, "machine")))

        self.assertEqual(
            [entry.profile_id for entry in catalog.select_filaments(dual, [FILAMENT_ID, WIDE_NOZZLE_FILAMENT_ID])],
            [FILAMENT_ID, WIDE_NOZZLE_FILAMENT_ID],
        )
        for filaments, code in (
            ([WIDE_NOZZLE_FILAMENT_ID, FILAMENT_ID], "incompatible_profile"),
            ([FILAMENT_ID, FILAMENT_ID, FILAMENT_ID], "too_many_filaments"),
            ([], "too_many_filaments"),
        ):
            with self.subTest(filaments=filaments), self.assertRaises(ProfileCatalogError) as raised:
                catalog.select_filaments(dual, filaments)
            self.assertEqual(raised.exception.code, code)

    def test_materializes_every_filament_of_a_multi_material_job(self):
        printer = self.catalog.get(MACHINE_ID, "machine")
        # One nozzle with a filament changer takes up to the desktop's limit.
        self.assertEqual(printer.describe()["max_filaments"], 16)
        self.assertEqual(self.catalog.get(PETG_ID, "filament").describe()["colour"], "#00AE42")
        selected = self.catalog.select_filaments(printer, [FILAMENT_ID, PETG_ID, FILAMENT_ID])
        written = self.catalog.materialize_filaments(selected, self.root / "job" / "filaments")
        self.assertEqual([path.name for path in written], ["filament-0.json", "filament-1.json", "filament-2.json"])
        self.assertEqual(json.loads(written[1].read_text(encoding="utf-8"))["filament_type"], ["PETG"])
        with self.assertRaises(ProfileCatalogError) as raised:
            self.catalog.select_filaments(printer, [FILAMENT_ID] * 17)
        self.assertEqual(raised.exception.code, "too_many_filaments")

    def test_records_the_inheritance_chain(self):
        entry = self.catalog.get(FILAMENT_ID, "filament")
        self.assertEqual(entry.inherits_chain, ("fdm_filament_pla", "Test Generic PLA"))
        self.assertEqual(entry.describe()["inherits_chain"], ["fdm_filament_pla", "Test Generic PLA"])

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
