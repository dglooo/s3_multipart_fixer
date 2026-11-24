#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path


DEFAULT_INPUT = "report/objects_parse.json"
DEFAULT_OUTPUT = "report/marker_paths.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Extract unique path values from report/objects_parse.json into a JSON array.")
    parser.add_argument("--input", default=DEFAULT_INPUT, help=f"Input JSON file. Default: {DEFAULT_INPUT}")
    parser.add_argument("--output", default=DEFAULT_OUTPUT, help=f"Output JSON file. Default: {DEFAULT_OUTPUT}")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    input_path = Path(args.input)
    output_path = Path(args.output)
    if output_path.parent == Path("."):
        output_path = Path("report") / output_path

    payload = json.loads(input_path.read_text(encoding="utf-8"))
    paths: set[str] = set()
    collect_paths(payload, paths)
    result = sorted(paths)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=4, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"written {output_path} | paths_count={len(result)}")
    return 0


def collect_paths(payload, paths: set[str]) -> None:
    if isinstance(payload, dict):
        path_value = payload.get("path")
        if isinstance(path_value, str):
            paths.add(path_value)
        for value in payload.values():
            collect_paths(value, paths)
        return
    if isinstance(payload, list):
        for item in payload:
            collect_paths(item, paths)


if __name__ == "__main__":
    raise SystemExit(main())
