#!/usr/bin/env python3
"""Check a published scene against the model and the blob it describes.

The plater draws what this produces, and a slice is later asked to reproduce
that placement, so the index and its binary companion must agree exactly. The
format is documented in `docs/web/scene-format.md`.
"""

import argparse
import json
import shutil
import struct
import subprocess
import tempfile
from pathlib import Path


POINT = struct.Struct("<iii")
VERTEX_BYTES = POINT.size


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Verify a published scene against its own blob.")
    parser.add_argument("--worker", required=True, type=Path)
    parser.add_argument("--model", required=True, type=Path)
    return parser.parse_args()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def inspect(worker: Path, model: Path, job_dir: Path) -> None:
    shutil.copyfile(model, job_dir / f"model{model.suffix}")
    manifest = {
        "protocol_version": 1,
        "job_id": "scene-smoke",
        "operation": {
            "name": "inspect",
            "version": 1,
            "payload": {"input_model": f"model{model.suffix}", "output_scene": "scene.json"},
        },
    }
    manifest_path = job_dir / "request.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    process = subprocess.run(
        [str(worker), "--inspect-manifest", str(manifest_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=300,
    )
    # The worker reports failures as events on stdout, so both streams are quoted.
    require(
        process.returncode == 0,
        f"the inspect failed with exit code {process.returncode}: {process.stderr}{process.stdout}",
    )


def decode_bounds(blob: bytes, offset: int, vertex_count: int, quantum: float) -> tuple:
    minimum = [float("inf")] * 3
    maximum = [float("-inf")] * 3
    for vertex in range(vertex_count):
        for axis, value in enumerate(POINT.unpack_from(blob, offset + vertex * VERTEX_BYTES)):
            millimetres = value * quantum
            minimum[axis] = min(minimum[axis], millimetres)
            maximum[axis] = max(maximum[axis], millimetres)
    return minimum, maximum


def main() -> None:
    args = parse_args()
    with tempfile.TemporaryDirectory(prefix="orca-scene-") as temporary:
        job_dir = Path(temporary)
        inspect(args.worker, args.model, job_dir)

        scene = json.loads((job_dir / "scene.json").read_text(encoding="utf-8"))
        blob = (job_dir / "scene.bin").read_bytes()
        result = json.loads((job_dir / "result.json").read_text(encoding="utf-8"))

        require(scene["scene_version"] == 1, "the scene declares an unexpected version")
        require(scene["units"] == "mm", "the scene is not in millimetres")
        require(scene["data"] == "scene.bin", "the scene names the wrong binary companion")
        require(scene["data_bytes"] == len(blob), "the index disagrees with the blob's size")
        require(bool(scene["bed"]["shape"]), "the scene declares no bed shape")
        require(scene["bed"]["printable_height"] > 0, "the scene declares no printable height")
        require(bool(scene["objects"]), "the scene declares no objects")

        require(result["outcome"] == "succeeded", "the inspect job did not succeed")
        require(
            sorted(artifact["kind"] for artifact in result["artifacts"]) == ["scene", "scene_data"],
            "the inspect job published something other than a scene and its data",
        )

        quantum = scene["quantum_mm"]
        expected_offset = 0
        for position, described in enumerate(scene["objects"]):
            require(described["index"] == position, "the scene's objects are not indexed in order")
            require(described["offset"] == expected_offset, "the scene's object ranges are not contiguous")
            require(
                described["vertex_count"] == described["triangle_count"] * 3,
                "an object's vertex count is not three per triangle",
            )
            require(
                described["length"] == described["vertex_count"] * VERTEX_BYTES,
                "an object's declared length does not match its vertex count",
            )
            require(len(described["transform"]) == 16, "an object's transform is not a 4x4 matrix")

            minimum, maximum = decode_bounds(blob, described["offset"], described["vertex_count"], quantum)
            for axis in range(3):
                # The quantum is one micrometre, so the declared box and the
                # decoded one may differ by at most one on each axis.
                require(
                    abs(described["bounding_box"]["min"][axis] - minimum[axis]) <= quantum,
                    f"object {position}'s declared minimum disagrees with its own vertices",
                )
                require(
                    abs(described["bounding_box"]["max"][axis] - maximum[axis]) <= quantum,
                    f"object {position}'s declared maximum disagrees with its own vertices",
                )
            expected_offset += described["length"]
        require(expected_offset == len(blob), "the scene's object ranges do not cover the blob")

    triangles = sum(described["triangle_count"] for described in scene["objects"])
    print(
        f"scene verified: {len(scene['objects'])} objects, {triangles} triangles, "
        f"{len(blob)} bytes, bed {len(scene['bed']['shape'])} points"
    )


if __name__ == "__main__":
    main()
