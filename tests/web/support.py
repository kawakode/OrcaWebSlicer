#!/usr/bin/env python3
"""Fixtures shared by the API tests: a bundled profile tree and a fake worker."""

import json
from pathlib import Path


# Slices the manifest the API actually wrote, so the tests exercise the real
# envelope. The mode argument selects a terminal outcome.
FAKE_WORKER = r'''#!/usr/bin/env python3
import hashlib
import json
import signal
import sys
import time
from pathlib import Path

mode = sys.argv[1]

# The API reads the engine's setting metadata and compatibility answers through
# the same executable it slices with, so the fake answers both too.
if sys.argv[2] == "--export-settings-catalog":
    print(Path(__file__).with_name("settings_catalog.json").read_text(encoding="utf-8"))
    raise SystemExit(0)
if sys.argv[2] == "--evaluate-compatibility":
    request = json.loads(Path(sys.argv[3]).read_text(encoding="utf-8"))
    for printer in request["printers"]:
        assert (Path(sys.argv[3]).parent / printer["profile"]).is_file(), "the printer was not staged"
    # A condition names the printers it accepts; the real worker runs the
    # engine's placeholder parser instead.
    answer = {
        candidate["id"]: [p["id"] for p in request["printers"] if p["name"] in candidate["condition"]]
        for candidate in request["candidates"]
    }
    print(json.dumps({"catalog_version": 1, "compatibility": answer, "unevaluated": []}))
    raise SystemExit(0)

manifest = Path(sys.argv[3])
request = json.loads(manifest.read_text(encoding="utf-8"))
job_id = request["job_id"]
payload = request["operation"]["payload"]
job_root = manifest.parent
sequence = 0


def event(kind, **fields):
    global sequence
    value = {"protocol_version": 1, "job_id": job_id, "sequence": sequence, "type": kind}
    value.update(fields)
    sequence += 1
    print(json.dumps(value, separators=(",", ":")), flush=True)


def publish(outcome, artifacts=None, error=None, warnings=()):
    value = {
        "protocol_version": 1,
        "job_id": job_id,
        "outcome": outcome,
        "warnings": list(warnings),
        "timing": {"duration_ms": 3, "cpu_time_ms": 2},
        "resource_usage": {"peak_memory_bytes": 1024},
        "artifacts": artifacts or [],
        "error": error,
    }
    (job_root / "result.json").write_text(json.dumps(value), encoding="utf-8")


# Every mode proves the request reached the worker intact.
assert (job_root / payload["input_model"]).is_file(), "the model was not staged"
for name in ("machine", "process", "filament"):
    assert (job_root / payload["profiles"][name]).is_file(), name + " profile was not staged"

event("state", state="accepted")
event("state", state="running")

if mode == "success":
    warnings = [{"code": "thin_wall", "message": "A thin wall was detected."}]
    event("progress", progress={"stage": "slicing", "percent": 50, "message": "Slicing"})
    event("warning", warning=warnings[0])
    target = job_root / payload["output_gcode"]
    target.parent.mkdir(parents=True, exist_ok=True)
    contents = ("; layer_height = " + payload["settings"].get("layer_height", "default") + "\nG1 X1\n")
    target.write_text(contents, encoding="utf-8")
    artifact = {
        "kind": "gcode",
        "path": payload["output_gcode"],
        "size_bytes": target.stat().st_size,
        "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
    }
    publish("succeeded", [artifact], warnings=warnings)
    event("artifact", artifact=artifact)
    event("state", state="succeeded")
elif mode == "fail":
    error = {"category": "slicing", "code": "slicing_failed", "message": "The model could not be sliced."}
    event("error", error=error)
    publish("failed", error=error)
    event("state", state="failed")
    raise SystemExit(5)
elif mode == "crash":
    raise SystemExit(9)
elif mode == "progress":
    # Report one stage and one warning, then wait to be canceled, so a caller
    # can observe both before any terminal result exists.
    event("progress", progress={"stage": "slicing", "percent": 42, "message": "Slicing"})
    event("warning", warning={"code": "slow_start", "message": "The first layer is slow."})
    signal.signal(signal.SIGTERM, lambda *unused: sys.exit(6))
    while True:
        time.sleep(0.05)
elif mode == "slow":
    signal.signal(signal.SIGTERM, lambda *unused: sys.exit(6))
    while True:
        time.sleep(0.05)
'''


_MACHINE_BASE = {
    "type": "machine",
    "name": "fdm_machine_common",
    "from": "system",
    "instantiation": "false",
    "printable_height": "250",
}
_PROCESS_BASE = {
    "type": "process",
    "name": "fdm_process_common",
    "from": "system",
    "instantiation": "false",
    "layer_height": "0.2",
    "sparse_infill_density": "15%",
}
_FILAMENT_BASE = {
    "type": "filament",
    "name": "fdm_filament_pla",
    "from": "system",
    "instantiation": "false",
    "filament_type": ["PLA"],
}

# A reduced copy of what `--export-settings-catalog` prints, in the same shape.
SETTINGS_CATALOG = {
    "catalog_version": 1,
    "engine_version": "test",
    "groups": [{"id": "quality", "label": "Quality"}, {"id": "support", "label": "Support"}],
    "settings": [
        {"key": "layer_height", "group": "quality", "scope": "process", "type": "float", "vector": False,
         "label": "Layer height", "unit": "mm", "min": 0, "default": "0.2"},
        {"key": "sparse_infill_density", "group": "quality", "scope": "process", "type": "percent",
         "vector": False, "label": "Sparse infill density", "unit": "%", "min": 0, "max": 100, "default": "20%"},
        {"key": "wall_loops", "group": "quality", "scope": "process", "type": "int", "vector": False,
         "label": "Wall loops", "min": 0, "max": 1000, "default": "2"},
        {"key": "filament_type", "group": "quality", "scope": "filament", "type": "strings", "vector": True,
         "label": "Type", "default": "PLA"},
        {"key": "enable_support", "group": "support", "scope": "process", "type": "bool", "vector": False,
         "label": "Enable support", "default": "0"},
        {"key": "support_type", "group": "support", "scope": "process", "type": "enum", "vector": False,
         "label": "Type", "enabled_by": "enable_support", "default": "normal(auto)",
         "enum": [{"value": "normal(auto)", "label": "normal(auto)"}, {"value": "tree(auto)", "label": "tree(auto)"}]},
    ],
}


def write_fake_worker(root: Path) -> Path:
    """Write the fake worker and the settings catalog it exports."""
    worker = Path(root) / "fake_worker.py"
    worker.write_text(FAKE_WORKER, encoding="utf-8")
    worker.with_name("settings_catalog.json").write_text(json.dumps(SETTINGS_CATALOG), encoding="utf-8")
    return worker


VENDOR = "Testing"
MACHINE_ID = f"{VENDOR}/machine/Test Printer 0.4 nozzle"
OTHER_MACHINE_ID = f"{VENDOR}/machine/Other Printer 0.4 nozzle"
PROCESS_ID = f"{VENDOR}/process/0.20mm Standard @Test"
FILAMENT_ID = f"{VENDOR}/filament/Test Generic PLA"
OTHER_PROCESS_ID = f"{VENDOR}/process/0.20mm Standard @Other"
CONDITIONAL_PROCESS_ID = f"{VENDOR}/process/0.28mm Draft @Conditional"


def write_profile_tree(repo_root: Path) -> Path:
    """Write a small bundled vendor whose shape matches `resources/profiles`."""
    profiles = Path(repo_root) / "resources" / "profiles"
    vendor = profiles / VENDOR
    for kind in ("machine", "process", "filament"):
        (vendor / kind).mkdir(parents=True, exist_ok=True)

    def write(kind, profile, directory=""):
        sub_path = f"{kind}/{directory}{profile['name']}.json"
        path = vendor / sub_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(profile), encoding="utf-8")
        return sub_path

    machine_base = write("machine", _MACHINE_BASE)
    write("process", _PROCESS_BASE)
    write("filament", _FILAMENT_BASE)

    listed = {"machine_list": [], "process_list": [], "filament_list": []}

    def publish(kind, profile, directory=""):
        listed[f"{kind}_list"].append(
            {"name": profile["name"], "sub_path": write(kind, profile, directory)}
        )

    publish("machine", {
        "type": "machine",
        "name": "Test Printer 0.4 nozzle",
        "inherits": "fdm_machine_common",
        "from": "system",
        "instantiation": "true",
        "printer_model": "Test Printer",
        "nozzle_diameter": ["0.4"],
        "default_print_profile": "0.20mm Standard @Test",
    })
    publish("machine", {
        "type": "machine",
        "name": "Other Printer 0.4 nozzle",
        "inherits": "fdm_machine_common",
        "from": "system",
        "instantiation": "true",
        "printer_model": "Other Printer",
        "nozzle_diameter": ["0.4"],
    })
    # A vendor lists its inheritance bases too; they are not user choices.
    listed["machine_list"].append({"name": "fdm_machine_common", "sub_path": machine_base})
    publish("process", {
        "type": "process",
        "name": "0.20mm Standard @Test",
        "inherits": "fdm_process_common",
        "from": "system",
        "instantiation": "true",
        "compatible_printers": ["Test Printer 0.4 nozzle"],
    })
    publish("process", {
        "type": "process",
        "name": "0.20mm Standard @Other",
        "inherits": "fdm_process_common",
        "from": "system",
        "instantiation": "true",
        "compatible_printers": ["Other Printer 0.4 nozzle"],
    })
    # Only the engine can evaluate a condition, so this one is answered by the
    # compatibility evaluator rather than by a declared list.
    publish("process", {
        "type": "process",
        "name": "0.28mm Draft @Conditional",
        "inherits": "fdm_process_common",
        "from": "system",
        "instantiation": "true",
        "layer_height": "0.28",
        "compatible_printers_condition": "printer_notes=~/.*Other Printer 0.4 nozzle.*/",
    })
    # Branded filaments sit in a subdirectory while their base stays one level
    # up, exactly as the bundled vendors ship them.
    publish("filament", {
        "type": "filament",
        "name": "Test Generic PLA",
        "inherits": "fdm_filament_pla",
        "from": "system",
        "instantiation": "true",
    }, "Branded/")
    listed["machine_list"].append({"name": "Escaping", "sub_path": "../../../etc/passwd"})

    index = {"name": VENDOR, "version": "01.00.00.00"}
    index.update(listed)
    (profiles / f"{VENDOR}.json").write_text(json.dumps(index), encoding="utf-8")
    return profiles
