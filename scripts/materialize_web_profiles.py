#!/usr/bin/env python3
"""Resolve Orca profile inheritance into job-local worker profile files."""

import argparse
import json
import pathlib

from web_baseline import resolve_profile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=pathlib.Path)
    parser.add_argument("--machine", required=True, type=pathlib.Path)
    parser.add_argument("--process", required=True, type=pathlib.Path)
    parser.add_argument("--filament", required=True, type=pathlib.Path)
    args = parser.parse_args()

    args.output.mkdir(parents=True, exist_ok=False)
    for label in ("machine", "process", "filament"):
        resolved, _ = resolve_profile(getattr(args, label))
        target = args.output / "{}.json".format(label)
        with target.open("w", encoding="utf-8") as stream:
            json.dump(resolved, stream, indent=2, sort_keys=True)
            stream.write("\n")


if __name__ == "__main__":
    main()
