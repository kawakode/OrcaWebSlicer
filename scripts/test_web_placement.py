#!/usr/bin/env python3
"""Verify that a slice request's explicit placement lands where the plater showed it.

This automates the second acceptance criterion in `docs/web/mvp.md`: "Submitted
transforms are applied in millimetres and reproduce the displayed placement
within a documented tolerance." It drives the real HTTP surface end to end —
upload, `POST /uploads/{id}/scene`, decode `scene.bin`, `POST /jobs` with an
explicit `objects` placement, download and summarize the G-code — which is the
round trip the browser makes. See docs/web/scene-format.md and docs/web/api.md.

Comparing G-code bounds to model bounds naively is unsound, so there are two
checks:

1. Differential (`DIFFERENTIAL_TOLERANCE_MM`, 0.01 mm): the same object is
   sliced at two placements differing by a known pure translation, and the two
   G-code XY boxes must differ by exactly that translation. Every
   extrusion-width, skirt, and brim effect is identical between the runs and
   cancels, so this is near-exact: the four components were measured to differ
   from the expected 40/25 mm by at most 1.4e-14 mm.

2. Absolute (`ABSOLUTE_TOLERANCE_MM`, 0.25 mm): with the skirt and brim
   disabled so the printed outline is the object itself, each G-code XY bound
   must sit within tolerance of the placed object's world box — computed here
   by decoding `scene.bin` and applying the submitted matrix, never by asking
   the engine. Measured: every one of the twelve components (four bounds
   across three placements) missed by exactly 0.20 mm, inward on every side,
   which is half the process's 0.4 mm `outer_wall_line_width` — the wall's
   centreline is inset from the mesh surface by half an extrusion width. The
   tolerance leaves a 25% margin over that.

Fixture: `tests/data/bridge.obj`, a 50 x 10 x 8 mm block. Its footprint is
deliberately not square — a cube's XY bounds survive a 90-degree Z rotation
unchanged, so a cube would pass a rotation check that rotated nothing. Its
local frame already rests at Z = 0, so the worker's `ensure_on_bed(true)`,
which lifts only an object that is *fully* below the bed, never rewrites what
this script asserts on.

Also checked: a duplicate `source_object` spans both placements and extrudes
clearly more than one copy, and an out-of-range `source_object` fails the job
with the stable `invalid_source_object` / `input` error and publishes nothing.
"""

import argparse
import json
import struct
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from web.api.config import ApiConfig  # noqa: E402
from web.api.app import create_app  # noqa: E402
from web_baseline import summarize_gcode  # noqa: E402
from web_job_directory import JobDirectoryLimits  # noqa: E402
from web_worker_executor import ExecutorLimits  # noqa: E402

TERMINAL_STATES = {"succeeded", "failed", "canceled"}

FIXTURE = REPO_ROOT / "tests" / "data" / "bridge.obj"
MACHINE_PROFILE = "Anycubic/machine/Anycubic Kobra 0.4 nozzle"
PROCESS_PROFILE = "Anycubic/process/0.20mm Standard @Anycubic Kobra"
FILAMENT_PROFILE = "Anycubic/filament/Anycubic Generic PLA"
# Disables the skirt and brim so the printed outline is the object itself,
# which the absolute check needs. layer_height stays at the profile default
# (0.2 mm over an 8 mm fixture = 40 layers), so the first-layer-only
# elefant_foot_compensation cannot bias the all-layer XY bounds this script
# measures.
NO_ADHESION_AIDS = {"skirt_loops": "0", "brim_type": "no_brim"}

# See the module docstring for what each tolerance accounts for and what was
# measured to justify it.
DIFFERENTIAL_TOLERANCE_MM = 0.01
ABSOLUTE_TOLERANCE_MM = 0.25

TRANSLATION_DELTA = (40.0, 25.0)  # (dx, dy) between the two differential placements

POINT = struct.Struct("<iii")
FIRST_LAYER_MARKER = b";LAYER_CHANGE"
MACHINE_GCODE_MARKER = b";TYPE:Custom"


class PlacementCheckFailed(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise PlacementCheckFailed(message)


def translate_mm(dx, dy, dz=0.0):
    """A column-major 4x4 identity-rotation transform, translated by (dx, dy, dz) mm."""
    return [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, dx, dy, dz, 1]


def rotate_z90_mm(dx, dy, dz=0.0):
    """A column-major 4x4 transform: rotate 90 degrees about Z, then translate."""
    # Column-major: column 0 is R*e_x = (0,1,0), column 1 is R*e_y = (-1,0,0).
    return [0, 1, 0, 0, -1, 0, 0, 0, 0, 0, 1, 0, dx, dy, dz, 1]


def decode_local_vertices(blob, quantum):
    count = len(blob) // POINT.size
    return [tuple(v * quantum for v in POINT.unpack_from(blob, index * POINT.size)) for index in range(count)]


def apply_transform(vertices, matrix):
    m = matrix
    return [
        (
            m[0] * x + m[4] * y + m[8] * z + m[12],
            m[1] * x + m[5] * y + m[9] * z + m[13],
            m[2] * x + m[6] * y + m[10] * z + m[14],
        )
        for x, y, z in vertices
    ]


def world_xy_bounds(vertices):
    xs = [vertex[0] for vertex in vertices]
    ys = [vertex[1] for vertex in vertices]
    return (min(xs), min(ys), max(xs), max(ys))


def union_xy_bounds(a, b):
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def strip_machine_gcode(raw):
    """Drop the machine profile's start/end G-code, keeping only the model's own toolpath.

    `machine_start_gcode` and `machine_end_gcode` in the bundled Anycubic
    Kobra machine profile draw fixed-position moves that have nothing to do
    with object placement: a purge/intro line from X5 to X205 near Y1, and a
    park move to a fixed X0 Y105 (see
    resources/profiles/Anycubic/machine/Anycubic Kobra 0.4 nozzle.json). Left
    in, they dominate `summarize_gcode`'s XY bounds regardless of where the
    object was placed. OrcaSlicer wraps exactly these two blocks in a
    `;TYPE:Custom` marker and always marks the first real layer with
    `;LAYER_CHANGE`, so keeping only what is between the first `;LAYER_CHANGE`
    and the last `;TYPE:Custom` isolates the model's own toolpath. This only
    narrows the bytes handed to `summarize_gcode`; its own parsing is unused
    and unmodified.
    """
    lines = raw.splitlines(keepends=True)
    starts = [index for index, line in enumerate(lines) if line.strip().startswith(FIRST_LAYER_MARKER)]
    customs = [index for index, line in enumerate(lines) if line.strip().startswith(MACHINE_GCODE_MARKER)]
    require(starts, "the G-code has no ;LAYER_CHANGE")
    require(len(customs) >= 2, "expected the machine start and end G-code to each be marked ;TYPE:Custom")
    return b"".join(lines[starts[0] : customs[-1]])


def require_box_close(label, expected, actual, tolerance):
    """Compare two (min_x, min_y, max_x, max_y) boxes component-wise."""
    for name, expected_value, actual_value in zip(("min_x", "min_y", "max_x", "max_y"), expected, actual):
        diff = abs(expected_value - actual_value)
        require(
            diff <= tolerance,
            f"{label} {name}: expected {expected_value:.4f} mm, measured {actual_value:.4f} mm, "
            f"difference {diff:.4f} mm exceeds tolerance {tolerance:.4f} mm",
        )


def wait_for_job(client, job_id, timeout_seconds):
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        job = client.get(f"/api/v1/jobs/{job_id}").json()
        if job["state"] in TERMINAL_STATES:
            return job
        time.sleep(0.05)
    raise PlacementCheckFailed(f"job {job_id} did not reach a terminal state within {timeout_seconds}s")


def upload_fixture(client):
    with FIXTURE.open("rb") as stream:
        response = client.post("/api/v1/uploads", files={"file": (FIXTURE.name, stream)})
    require(response.status_code == 201, f"upload failed: {response.status_code} {response.text}")
    return response.json()["upload_id"]


def fetch_scene(client, upload_id, timeout_seconds):
    submitted = client.post(
        f"/api/v1/uploads/{upload_id}/scene",
        json={
            "machine_profile": MACHINE_PROFILE,
            "process_profile": PROCESS_PROFILE,
            "filament_profile": FILAMENT_PROFILE,
        },
    )
    require(submitted.status_code == 202, f"scene request failed: {submitted.status_code} {submitted.text}")
    job = wait_for_job(client, submitted.json()["job_id"], timeout_seconds)
    require(job["state"] == "succeeded", f"scene job did not succeed: state={job['state']} error={job['error']}")

    index = client.get(f"/api/v1/scenes/{job['job_id']}")
    require(index.status_code == 200, f"scene index request failed: {index.status_code}")
    scene = index.json()
    require(len(scene["objects"]) == 1, f"expected one scene object for {FIXTURE.name}, found {len(scene['objects'])}")

    described = scene["objects"][0]
    blob = client.get(f"/api/v1/scenes/{job['job_id']}/objects/0")
    require(blob.status_code == 200, f"scene object request failed: {blob.status_code}")
    require(
        len(blob.content) == described["length"],
        f"scene object byte length mismatch: index says {described['length']}, got {len(blob.content)}",
    )

    vertices = decode_local_vertices(blob.content, scene["quantum_mm"])
    return len(scene["objects"]), vertices


def submit_slice(client, upload_id, objects, settings, timeout_seconds):
    accepted = client.post(
        "/api/v1/jobs",
        json={
            "upload_id": upload_id,
            "machine_profile": MACHINE_PROFILE,
            "process_profile": PROCESS_PROFILE,
            "filament_profile": FILAMENT_PROFILE,
            "settings": settings,
            "objects": objects,
        },
    )
    require(accepted.status_code == 202, f"slice request failed: {accepted.status_code} {accepted.text}")
    return wait_for_job(client, accepted.json()["job_id"], timeout_seconds)


def sliced_summary(client, upload_id, objects, output_root, label, timeout_seconds):
    job = submit_slice(client, upload_id, objects, NO_ADHESION_AIDS, timeout_seconds)
    require(job["state"] == "succeeded", f"{label} slice did not succeed: state={job['state']} error={job['error']}")
    downloaded = client.get(f"/api/v1/jobs/{job['job_id']}/artifacts/gcode")
    require(downloaded.status_code == 200, f"{label} G-code download failed: {downloaded.status_code}")
    gcode_path = output_root / f"{label}.gcode"
    gcode_path.write_bytes(strip_machine_gcode(downloaded.content))
    return summarize_gcode(gcode_path)


def check_invalid_source_object(client, upload_id, object_count, timeout_seconds):
    job = submit_slice(
        client,
        upload_id,
        [{"source_object": object_count, "transform": translate_mm(0, 0, 0)}],
        {},
        timeout_seconds,
    )
    require(job["state"] == "failed", f"a bad source_object should fail the job, got state={job['state']}")
    error = job["error"] or {}
    require(
        error.get("code") == "invalid_source_object",
        f"expected error code invalid_source_object, got {error.get('code')!r}",
    )
    require(
        error.get("category") == "input",
        f"expected error category input, got {error.get('category')!r}",
    )
    artifact_names = {artifact["name"] for artifact in job["artifacts"]}
    require("gcode" not in artifact_names, f"a failed job should publish no G-code, got artifacts={artifact_names}")
    return error


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--worker", required=True, type=Path)
    parser.add_argument("--timeout-seconds", type=float, default=300.0)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="orca-web-placement-") as workspace:
        root = Path(workspace)
        config = ApiConfig(
            repo_root=REPO_ROOT,
            state_root=root / "state",
            worker_command=(str(args.worker.resolve()),),
            profile_vendors=("Anycubic",),
            max_concurrent_jobs=1,
            executor_limits=ExecutorLimits(
                wall_time_ms=int(args.timeout_seconds * 1000),
                cpu_time_ms=int(args.timeout_seconds * 1000),
            ),
            job_limits=JobDirectoryLimits(),
        )
        with TestClient(create_app(config)) as client:
            upload_id = upload_fixture(client)
            object_count, local_vertices = fetch_scene(client, upload_id, args.timeout_seconds)

            # The fixture's own frame sits at x 75..125, y 84.5..94.5, so each
            # translation below is chosen to land the block well inside the
            # 220 mm bed, and the two duplicates far enough apart to be
            # distinguishable in the combined bounds.
            place_a = translate_mm(-25.0, 20.0, 0.0)
            place_b = translate_mm(-25.0 + TRANSLATION_DELTA[0], 20.0 + TRANSLATION_DELTA[1], 0.0)
            place_rot = rotate_z90_mm(154.5, -15.0, 0.0)
            place_dup = translate_mm(90.0, 20.0, 0.0)

            expected_a = world_xy_bounds(apply_transform(local_vertices, place_a))
            expected_b = world_xy_bounds(apply_transform(local_vertices, place_b))
            expected_rot = world_xy_bounds(apply_transform(local_vertices, place_rot))
            expected_dup = world_xy_bounds(apply_transform(local_vertices, place_dup))

            summary_a = sliced_summary(
                client, upload_id, [{"source_object": 0, "transform": place_a}], root, "place-a", args.timeout_seconds
            )
            summary_b = sliced_summary(
                client, upload_id, [{"source_object": 0, "transform": place_b}], root, "place-b", args.timeout_seconds
            )
            summary_rot = sliced_summary(
                client,
                upload_id,
                [{"source_object": 0, "transform": place_rot}],
                root,
                "place-rot",
                args.timeout_seconds,
            )
            summary_dup = sliced_summary(
                client,
                upload_id,
                [
                    {"source_object": 0, "transform": place_a},
                    {"source_object": 0, "transform": place_dup},
                ],
                root,
                "place-dup",
                args.timeout_seconds,
            )

            # 1. Differential check: extrusion-width and skirt/brim effects are
            # identical between the two runs and cancel exactly in the difference.
            measured_delta = tuple(b - a for a, b in zip(summary_a["xy_bounds"], summary_b["xy_bounds"]))
            expected_delta = (TRANSLATION_DELTA[0], TRANSLATION_DELTA[1], TRANSLATION_DELTA[0], TRANSLATION_DELTA[1])
            require_box_close("differential", expected_delta, measured_delta, DIFFERENTIAL_TOLERANCE_MM)

            # 2. Absolute check: translation and rotation, each against a
            # world box computed independently from the decoded mesh.
            require_box_close("absolute translation A", expected_a, summary_a["xy_bounds"], ABSOLUTE_TOLERANCE_MM)
            require_box_close("absolute translation B", expected_b, summary_b["xy_bounds"], ABSOLUTE_TOLERANCE_MM)
            require_box_close("absolute rotation", expected_rot, summary_rot["xy_bounds"], ABSOLUTE_TOLERANCE_MM)

            # Duplicates: the combined box spans both placements, and printing
            # two copies clearly extrudes more than printing one.
            expected_union = union_xy_bounds(expected_a, expected_dup)
            require_box_close("duplicate union", expected_union, summary_dup["xy_bounds"], ABSOLUTE_TOLERANCE_MM)
            single_extrusion = sum(summary_a["positive_extrusion_by_tool"].values())
            duplicate_extrusion = sum(summary_dup["positive_extrusion_by_tool"].values())
            require(
                duplicate_extrusion > single_extrusion * 1.5,
                f"duplicate extrusion {duplicate_extrusion:.4f} mm should clearly exceed "
                f"1.5x a single copy's {single_extrusion:.4f} mm (got {duplicate_extrusion / single_extrusion:.3f}x)",
            )

            invalid_error = check_invalid_source_object(client, upload_id, object_count, args.timeout_seconds)

    report = {
        "fixture": str(FIXTURE.relative_to(REPO_ROOT)),
        "tolerances_mm": {"differential": DIFFERENTIAL_TOLERANCE_MM, "absolute": ABSOLUTE_TOLERANCE_MM},
        "measured": {
            "differential_delta": measured_delta,
            "expected_delta": expected_delta,
            "translation_a": {"expected": expected_a, "measured": summary_a["xy_bounds"]},
            "translation_b": {"expected": expected_b, "measured": summary_b["xy_bounds"]},
            "rotation": {"expected": expected_rot, "measured": summary_rot["xy_bounds"]},
            "duplicate_union": {"expected": expected_union, "measured": summary_dup["xy_bounds"]},
            "duplicate_extrusion_ratio": duplicate_extrusion / single_extrusion,
        },
        "invalid_source_object_error": invalid_error,
        "matches": True,
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
