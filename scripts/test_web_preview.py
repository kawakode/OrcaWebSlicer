#!/usr/bin/env python3
"""Check a published layer preview against the G-code it was generated from.

This is functional acceptance criterion 7 in `docs/web/mvp.md`: the preview must
agree with the produced G-code on layer count, Z range, tools, and extrusion
roles. The format itself is documented in `docs/web/preview-format.md`.
"""

import argparse
import json
import re
import shutil
import struct
import subprocess
import tempfile
from pathlib import Path


HEADER = struct.Struct("<BBBBIff")
POINT = struct.Struct("<iii")
KIND_TRAVEL = 1
Z_TOLERANCE_MM = 1e-3

# A non-BBL printer emits the compatible tags; a BBL one emits the Orca tags.
LAYER_MARKERS = (";LAYER_CHANGE", "; CHANGE_LAYER")
ROLE_MARKERS = (";TYPE:", "; FEATURE: ")
Z_PATTERN = re.compile(r"^(?:;Z:|; Z_HEIGHT: )(-?\d+(?:\.\d+)?)")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Verify the layer preview against its G-code.")
    parser.add_argument("--worker", required=True, type=Path)
    parser.add_argument("--model", required=True, type=Path)
    return parser.parse_args()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def slice_with_preview(worker: Path, model: Path, job_dir: Path) -> None:
    shutil.copyfile(model, job_dir / f"model{model.suffix}")
    manifest = {
        "protocol_version": 1,
        "job_id": "preview-smoke",
        "operation": {
            "name": "slice",
            "version": 1,
            "payload": {
                "input_model": f"model{model.suffix}",
                "output_gcode": "result.gcode",
                "output_preview": "preview.json",
                # Supports and a brim put several extrusion roles in one job, so
                # the role agreement is checked against more than walls.
                "settings": {
                    "layer_height": "0.3",
                    "initial_layer_print_height": "0.3",
                    # The bare engine defaults use relative extruder addressing,
                    # which the engine refuses without a per-layer reset.
                    "layer_change_gcode": "G92 E0\n",
                    "enable_support": "1",
                    "support_type": "normal(auto)",
                    "support_threshold_angle": "80",
                    "brim_type": "outer_only",
                    "brim_width": "3",
                },
            },
        },
    }
    manifest_path = job_dir / "request.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    process = subprocess.run(
        [str(worker), "--slice-manifest", str(manifest_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=600,
    )
    # The worker reports failures as events on stdout, so both streams are quoted.
    require(
        process.returncode == 0,
        f"the slice failed with exit code {process.returncode}: {process.stderr}{process.stdout}",
    )


def read_gcode(path: Path) -> dict:
    """Collect what the G-code itself prints: layers, and the roles and tools
    that actually extrude.

    A `;TYPE:` marker alone is not enough: the custom start and end G-code carry
    one without necessarily extruding, and the preview only records moves that
    lay down material. So a role counts only once a move under it advances the
    extruder while the head is moving.
    """
    layer_zs: list[float] = []
    roles: set[str] = set()
    tools: set[int] = set()
    pending_z = None
    role = ""
    tool = 0
    absolute_e = True
    last_e = 0.0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        code = line.split(";", 1)[0].strip()
        if line.startswith(LAYER_MARKERS):
            if pending_z is not None:
                layer_zs.append(pending_z)
            pending_z = None
        matched = Z_PATTERN.match(line)
        if matched:
            pending_z = float(matched.group(1))
        for marker in ROLE_MARKERS:
            if line.startswith(marker):
                role = line[len(marker):].strip()
        if not code:
            continue
        words = code.split()
        if re.fullmatch(r"T(\d+)", words[0]):
            tool = int(words[0][1:])
        elif words[0] == "M82":
            absolute_e = True
        elif words[0] == "M83":
            absolute_e = False
        elif words[0] == "G92":
            for word in words[1:]:
                if word.startswith("E"):
                    last_e = float(word[1:])
        elif words[0] in {"G0", "G1"}:
            extrusion = None
            moved = False
            for word in words[1:]:
                if word[:1] in {"X", "Y"}:
                    moved = True
                elif word[:1] == "E":
                    extrusion = float(word[1:])
            if extrusion is not None:
                if moved and (extrusion - last_e if absolute_e else extrusion) > 0:
                    roles.add(role)
                    tools.add(tool)
                last_e = extrusion if absolute_e else last_e + extrusion
    if pending_z is not None:
        layer_zs.append(pending_z)
    return {"layer_zs": layer_zs, "roles": roles, "tools": tools}


def decode_layer(blob: bytes) -> dict:
    """Decode one layer's byte range, proving it holds whole batches only."""
    offset = 0
    roles: set[int] = set()
    tools: set[int] = set()
    segments = 0
    while offset < len(blob):
        require(offset + HEADER.size <= len(blob), "a layer range ends inside a batch header")
        kind, role, tool, reserved, count, width, height = HEADER.unpack_from(blob, offset)
        require(reserved == 0, "a batch header uses its reserved byte")
        require(count >= 2, "a batch holds fewer than two points")
        offset += HEADER.size
        require(offset + count * POINT.size <= len(blob), "a layer range ends inside a batch's points")
        offset += count * POINT.size
        segments += count - 1
        if kind != KIND_TRAVEL:
            require(width > 0, "an extrusion batch declares no width")
            require(height > 0, "an extrusion batch declares no height")
            roles.add(role)
            tools.add(tool)
    return {"roles": roles, "tools": tools, "segments": segments}


def main() -> None:
    args = parse_args()
    with tempfile.TemporaryDirectory(prefix="orca-preview-") as temporary:
        job_dir = Path(temporary)
        slice_with_preview(args.worker, args.model, job_dir)

        index = json.loads((job_dir / "preview.json").read_text(encoding="utf-8"))
        data = (job_dir / "preview.bin").read_bytes()
        gcode = read_gcode(job_dir / "result.gcode")

        require(index["preview_version"] == 1, "the preview declares an unexpected version")
        require(index["units"] == "mm", "the preview is not in millimetres")
        require(index["data_bytes"] == len(data), "the index disagrees with the blob's size")
        require(bool(index["layers"]), "the preview declares no layers")

        # 1. Layer count and every layer's Z must match the G-code.
        preview_zs = [layer["z"] for layer in index["layers"]]
        require(
            len(preview_zs) == len(gcode["layer_zs"]),
            f"the preview has {len(preview_zs)} layers but the G-code prints {len(gcode['layer_zs'])}",
        )
        for expected, actual in zip(sorted(gcode["layer_zs"]), sorted(preview_zs)):
            require(
                abs(expected - actual) <= Z_TOLERANCE_MM,
                f"a preview layer is at z={actual} but the G-code prints that layer at z={expected}; "
                f"preview {sorted(preview_zs)[:8]} vs G-code {sorted(gcode['layer_zs'])[:8]}",
            )
        bounds = index["bounding_box"]
        require(
            abs(bounds["max"][2] - max(preview_zs)) <= Z_TOLERANCE_MM,
            "the preview's bounding box disagrees with its own top layer",
        )

        # 2. Every layer's byte range decodes exactly, and the ranges tile the blob.
        role_ids = [role["id"] for role in index["roles"]]
        labels = {role["id"]: role["label"] for role in index["roles"]}
        expected_offset = 0
        seen_roles: set[str] = set()
        seen_tools: set[int] = set()
        total_segments = 0
        for position, layer in enumerate(index["layers"]):
            require(layer["index"] == position, "the preview's layers are not indexed in order")
            require(layer["offset"] == expected_offset, "the preview's layer ranges are not contiguous")
            decoded = decode_layer(data[layer["offset"]:layer["offset"] + layer["length"]])
            require(
                decoded["segments"] == layer["segments"],
                "a layer's declared segment count disagrees with its bytes",
            )
            require(
                {role_ids[role] for role in decoded["roles"]} == set(layer["roles"]),
                "a layer's declared roles disagree with its bytes",
            )
            require(decoded["tools"] == set(layer["tools"]), "a layer's declared tools disagree with its bytes")
            seen_roles |= set(layer["roles"])
            seen_tools |= set(layer["tools"])
            total_segments += decoded["segments"]
            expected_offset += layer["length"]
        require(expected_offset == len(data), "the preview's layer ranges do not cover the blob")
        require(total_segments == index["segment_count"], "the preview miscounts its own segments")

        # 3. Tools and extrusion roles must match what the G-code declares.
        require(seen_tools == gcode["tools"], f"the preview uses tools {seen_tools}, the G-code {gcode['tools']}")
        require(set(index["tools"]) == seen_tools, "the index's tool list disagrees with its layers")
        previewed_labels = {labels[role] for role in seen_roles}
        require(
            previewed_labels == gcode["roles"],
            f"the preview shows roles {sorted(previewed_labels)} but the G-code emits {sorted(gcode['roles'])}",
        )
        # The fixture is deliberately configured to produce more than walls.
        require(len(previewed_labels) >= 3, "the fixture produced too few roles to be a useful check")

    print(
        f"preview verified: {len(index['layers'])} layers, {index['segment_count']} segments, "
        f"{len(previewed_labels)} roles, tools {sorted(seen_tools)}"
    )


if __name__ == "__main__":
    main()
