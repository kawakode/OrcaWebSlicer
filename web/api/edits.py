"""Object tools: a disposable worker edits one placed object into new models.

The API never touches geometry. It names the tool and the object's placement,
and each model the worker publishes becomes a new upload, inspected like any
other, so the plater draws an edited part exactly as it draws a fresh file.
"""

from __future__ import annotations

import dataclasses
from pathlib import PurePosixPath
from typing import Any, Dict, List, Optional, Tuple

TOOLS = ("cut", "repair")
CUT_SIDES = ("both", "upper", "lower")
# A cut produces at most an upper and a lower part.
OUTPUT_MODELS = ("output/part-0.stl", "output/part-1.stl")
OUTPUT_EDIT_REPORT = "output/edit.json"


@dataclasses.dataclass(frozen=True)
class EditSpec:
    """One tool applied to one object, placed by a row-major 4x4 transform.

    A cut plane is a point and a normal in plate millimetres; the normal points
    at the upper part.
    """

    tool: str
    transform: Tuple[float, ...]
    object_index: int = 0
    point: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    normal: Tuple[float, float, float] = (0.0, 0.0, 1.0)
    keep: str = "both"
    flip_upper: bool = False
    place_on_cut: bool = False

    def _cut(self) -> Dict[str, Any]:
        return {
            "point": list(self.point),
            "normal": list(self.normal),
            "keep": self.keep,
            "flip_upper": self.flip_upper,
            "place_on_cut": self.place_on_cut,
        }

    def describe(self) -> Dict[str, Any]:
        described: Dict[str, Any] = {"tool": self.tool, "object": self.object_index, "transform": list(self.transform)}
        if self.tool == "cut":
            described["cut"] = self._cut()
        return described

    def payload(self, model: str) -> Dict[str, Any]:
        """The worker's `edit` operation payload for the staged `model`."""
        payload = {
            "input_model": model,
            "object": self.object_index,
            "transform": list(self.transform),
            "tool": self.tool,
            "output_models": list(OUTPUT_MODELS),
            "output_report": OUTPUT_EDIT_REPORT,
        }
        if self.tool == "cut":
            payload["cut"] = self._cut()
        return payload


def edit_error(spec: Optional[EditSpec]) -> Optional[str]:
    """Why an edit cannot reach a worker, or None. The worker checks it again."""
    if spec is None or spec.tool not in TOOLS:
        return f"tool must be one of {', '.join(TOOLS)}."
    if len(spec.transform) != 16:
        return "transform is a row-major 4x4 matrix."
    if spec.tool == "cut":
        if spec.keep not in CUT_SIDES:
            return f"cut.keep must be one of {', '.join(CUT_SIDES)}."
        if sum(component * component for component in spec.normal) < 1e-12:
            return "cut.normal must not be zero."
    return None


def published_parts(result: Dict[str, Any], report: Dict[str, Any], source_filename: str) -> List[Tuple[str, str]]:
    """Pair each published model path with the filename its new upload takes.

    A part is named after the file it came from and its side of a cut, such as
    `bracket (upper).stl`, so the plate lists it recognisably.
    """
    sides = {part.get("model"): str(part.get("side") or "") for part in report.get("parts", []) if isinstance(part, dict)}
    stem = PurePosixPath(source_filename).stem or "model"
    parts = []
    for artifact in result.get("artifacts", []):
        if isinstance(artifact, dict) and artifact.get("kind") == "model":
            side = sides.get(artifact.get("path"), "")
            suffix = "repaired" if side in ("", "whole") else side
            parts.append((artifact.get("path"), f"{stem} ({suffix}).stl"))
    return parts
