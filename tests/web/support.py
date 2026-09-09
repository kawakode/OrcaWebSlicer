#!/usr/bin/env python3
"""Fixtures shared by the API tests: a bundled profile tree and a fake worker."""

import json
from pathlib import Path


# Slices the manifest the API actually wrote, so the tests exercise the real
# envelope. The mode argument selects a terminal outcome. The engine-metadata
# commands answer in the same shape the C++ worker uses, with a deliberately
# small catalog: the real definitions are covered by the worker's own tests.
FAKE_WORKER = r'''#!/usr/bin/env python3
import hashlib
import json
import signal
import struct
import sys
import time
from pathlib import Path

SETTINGS_CATALOG = {
    "catalog_version": 1,
    "engine_version": "0.0.0-test",
    "groups": [{"id": "quality", "label": "Quality"}, {"id": "support", "label": "Support"}],
    "settings": [
        {"key": "layer_height", "group": "quality", "scope": "process", "type": "float",
         "vector": False, "nullable": False, "mode": "simple", "label": "Layer height",
         "category": "Quality", "tooltip": "", "unit": "mm", "min": 0.01, "max": 0.6,
         "default": "0.2"},
        {"key": "wall_loops", "group": "quality", "scope": "process", "type": "int",
         "vector": False, "nullable": False, "mode": "simple", "label": "Wall loops",
         "category": "Quality", "tooltip": "", "unit": "", "min": 0, "max": 1000,
         "default": "2"},
        {"key": "enable_support", "group": "support", "scope": "process", "type": "bool",
         "vector": False, "nullable": False, "mode": "simple", "label": "Enable support",
         "category": "Support", "tooltip": "", "unit": "", "default": "0"},
        {"key": "support_type", "group": "support", "scope": "process", "type": "enum",
         "vector": False, "nullable": False, "mode": "simple", "label": "Support type",
         "category": "Support", "tooltip": "", "unit": "", "enabled_by": "enable_support",
         "enum": [{"value": "normal(auto)", "label": "Normal (auto)"},
                  {"value": "tree(auto)", "label": "Tree (auto)"}],
         "default": "normal(auto)"},
        {"key": "nozzle_temperature", "group": "quality", "scope": "filament", "type": "ints",
         "vector": True, "nullable": False, "mode": "simple", "label": "Nozzle temperature",
         "category": "Filament", "tooltip": "", "unit": "℃", "min": 0, "max": 500,
         "default": "220"},
    ],
}

mode = sys.argv[1]
command = sys.argv[2]

if command == "--export-settings-catalog":
    if mode == "no-metadata":
        print("the engine could not describe its settings", file=sys.stderr)
        raise SystemExit(8)
    print(json.dumps(SETTINGS_CATALOG), flush=True)
    raise SystemExit(0)

if command == "--evaluate-compatibility":
    # Stands in for the placeholder parser: a condition matches every printer
    # whose name contains it, which is enough to prove the wiring.
    query = json.loads(Path(sys.argv[3]).read_text(encoding="utf-8"))
    assert query["catalog_version"] == 1, "unexpected compatibility request version"
    for printer in query["printers"]:
        assert (Path(sys.argv[3]).parent / printer["profile"]).is_file(), "printer profile was not staged"
    print(json.dumps({
        "catalog_version": 1,
        "compatibility": {
            candidate["id"]: [
                printer["id"] for printer in query["printers"]
                if candidate["condition"] in printer["name"]
            ]
            for candidate in query["candidates"]
        },
        "unevaluated": [],
    }), flush=True)
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


# Every mode proves the request reached the worker intact. An inspect payload
# names only the machine profile, so the staged set is read from the payload
# itself rather than assumed to be the slice payload's fixed set. A slice
# payload's "filaments" is always the array form (one path per slot, 1-16).
assert (job_root / payload["input_model"]).is_file(), "the model was not staged"
for name, relative in payload["profiles"].items():
    if isinstance(relative, list):
        assert relative, name + " named no slots"
        for item in relative:
            assert (job_root / item).is_file(), name + " profile was not staged"
    else:
        assert (job_root / relative).is_file(), name + " profile was not staged"

event("state", state="accepted")
event("state", state="running")

def describe(path):
    return {
        "path": str(path.relative_to(job_root).as_posix()),
        "size_bytes": path.stat().st_size,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def write_preview(relative):
    """Two one-batch layers, in the format docs/web/preview-format.md defines."""
    index_path = job_root / relative
    data_path = index_path.with_suffix(".bin")
    index_path.parent.mkdir(parents=True, exist_ok=True)
    layers = []
    blob = b""
    for position, z in enumerate((0.2, 0.4)):
        points = [(0, 0, int(z * 1000)), (20000, 0, int(z * 1000)), (20000, 20000, int(z * 1000))]
        batch = struct.pack("<BBBBIff", 0, 2, 0, 0, len(points), 0.42, 0.2)
        batch += b"".join(struct.pack("<iii", *point) for point in points)
        layers.append({"index": position, "z": z, "offset": len(blob), "length": len(batch),
                       "segments": len(points) - 1, "roles": ["outer_wall"], "tools": [0]})
        blob += batch
    data_path.write_bytes(blob)
    index_path.write_text(json.dumps({
        "preview_version": 1,
        "units": "mm",
        "quantum_mm": 0.001,
        "data": str(data_path.relative_to(job_root).as_posix()),
        "data_bytes": len(blob),
        "segment_count": sum(layer["segments"] for layer in layers),
        "tools": [0],
        "roles": [{"id": "none", "label": "Undefined"}, {"id": "inner_wall", "label": "Inner wall"},
                  {"id": "outer_wall", "label": "Outer wall"}],
        "bounding_box": {"min": [0, 0, 0.2], "max": [20, 20, 0.4]},
        "layers": layers,
    }), encoding="utf-8")
    return [dict(describe(index_path), kind="preview"), dict(describe(data_path), kind="preview_data")]


def write_scene(relative):
    """Two one-triangle objects, in the format the WORKER CONTRACT defines."""
    index_path = job_root / relative
    data_path = index_path.with_suffix(".bin")
    index_path.parent.mkdir(parents=True, exist_ok=True)
    quantum = 0.001
    identity = [1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0]
    triangles = [
        [(0, 0, 0), (10000, 0, 0), (0, 10000, 0)],
        [(0, 0, 5000), (5000, 0, 5000), (0, 5000, 5000)],
    ]
    objects = []
    blob = b""
    for index, points in enumerate(triangles):
        body = b"".join(struct.pack("<iii", *point) for point in points)
        xs, ys, zs = zip(*points)
        objects.append({
            "index": index,
            "name": "object-%d" % index,
            "triangle_count": len(points) // 3,
            "bounding_box": {
                "min": [min(xs) * quantum, min(ys) * quantum, min(zs) * quantum],
                "max": [max(xs) * quantum, max(ys) * quantum, max(zs) * quantum],
            },
            "transform": identity,
            "offset": len(blob),
            "length": len(body),
            "vertex_count": len(points),
        })
        blob += body
    data_path.write_bytes(blob)
    index_path.write_text(json.dumps({
        "scene_version": 1,
        "units": "mm",
        "quantum_mm": quantum,
        "bed": {"shape": [[0, 0], [250, 0], [250, 220], [0, 220]], "printable_height": 250.0},
        "data": str(data_path.relative_to(job_root).as_posix()),
        "data_bytes": len(blob),
        "objects": objects,
    }), encoding="utf-8")
    return [dict(describe(index_path), kind="scene"), dict(describe(data_path), kind="scene_data")]


if mode == "success" and command == "--inspect-manifest":
    artifacts = write_scene(payload["output_scene"])
    publish("succeeded", artifacts)
    for artifact in artifacts:
        event("artifact", artifact=artifact)
    event("state", state="succeeded")
elif mode == "success":
    warnings = [{"code": "thin_wall", "message": "A thin wall was detected."}]
    event("progress", progress={"stage": "slicing", "percent": 50, "message": "Slicing"})
    event("warning", warning=warnings[0])
    target = job_root / payload["output_gcode"]
    target.parent.mkdir(parents=True, exist_ok=True)
    contents = (
        "; layer_height = " + payload["settings"].get("layer_height", "default") + "\n"
        "; objects = " + json.dumps(payload.get("objects", []), separators=(",", ":")) + "\n"
        "; profiles = " + json.dumps(payload["profiles"], separators=(",", ":")) + "\n"
        "G1 X1\n"
    )
    target.write_text(contents, encoding="utf-8")
    artifacts = [dict(describe(target), kind="gcode")]
    if payload.get("output_preview"):
        artifacts += write_preview(payload["output_preview"])
    publish("succeeded", artifacts, warnings=warnings)
    for artifact in artifacts:
        event("artifact", artifact=artifact)
    event("state", state="succeeded")
elif mode == "fail" and command == "--inspect-manifest":
    error = {"category": "inspection", "code": "inspection_failed", "message": "The model could not be inspected."}
    event("error", error=error)
    publish("failed", error=error)
    event("state", state="failed")
    raise SystemExit(5)
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

VENDOR = "Testing"
MACHINE_ID = f"{VENDOR}/machine/Test Printer 0.4 nozzle"
OTHER_MACHINE_ID = f"{VENDOR}/machine/Other Printer 0.4 nozzle"
PROCESS_ID = f"{VENDOR}/process/0.20mm Standard @Test"
FILAMENT_ID = f"{VENDOR}/filament/Test Generic PLA"
OTHER_PROCESS_ID = f"{VENDOR}/process/0.20mm Standard @Other"
# Declares only an expression, so its compatibility needs the engine.
CONDITIONAL_PROCESS_ID = f"{VENDOR}/process/0.20mm Conditional @Test"
CONDITIONAL_EXPRESSION = "Test Printer"


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
    # No declared list, so only the engine's expression evaluator can say which
    # printers this one suits.
    publish("process", {
        "type": "process",
        "name": "0.20mm Conditional @Test",
        "inherits": "fdm_process_common",
        "from": "system",
        "instantiation": "true",
        "compatible_printers_condition": CONDITIONAL_EXPRESSION,
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
