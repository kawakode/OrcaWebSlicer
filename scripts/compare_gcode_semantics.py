#!/usr/bin/env python3
"""Compare two G-code files using the web baseline's semantic contract."""

import argparse
import json
import pathlib

from web_baseline import summarize_gcode


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("expected", type=pathlib.Path)
    parser.add_argument("actual", type=pathlib.Path)
    args = parser.parse_args()

    expected = summarize_gcode(args.expected)
    actual = summarize_gcode(args.actual)
    if expected == actual:
        print(json.dumps({"matches": True}, sort_keys=True))
        return 0

    differences = {
        key: {"expected": expected.get(key), "actual": actual.get(key)}
        for key in sorted(set(expected) | set(actual))
        if expected.get(key) != actual.get(key)
    }
    print(json.dumps({"matches": False, "differences": differences}, indent=2, sort_keys=True))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
