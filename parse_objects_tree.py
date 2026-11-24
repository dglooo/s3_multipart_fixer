#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


DEFAULT_INPUT = "report/objects_.json"
DEFAULT_OUTPUT = "report/objects_parse.json"
DEFAULT_THRESHOLD_GB = 3000.0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Extract the largest directory paths from a count-objects JSON tree.",
    )
    parser.add_argument("--input", default=DEFAULT_INPUT, help=f"Input JSON file. Default: {DEFAULT_INPUT}")
    parser.add_argument("--output", default=DEFAULT_OUTPUT, help=f"Output JSON file. Default: {DEFAULT_OUTPUT}")
    parser.add_argument(
        "--min-size-gb",
        type=float,
        default=DEFAULT_THRESHOLD_GB,
        help=f"Keep only nodes with total_size_gb greater than this threshold. Default: {DEFAULT_THRESHOLD_GB}",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    input_path = Path(args.input)
    output_path = Path(args.output)
    if output_path.parent == Path("."):
        output_path = Path("report") / output_path

    payload = json.loads(input_path.read_text(encoding="utf-8"))
    matches, data_subtrees = extract_large_nodes(payload=payload, min_size_gb=args.min_size_gb)
    result = {
        "input_file": str(input_path),
        "output_file": str(output_path),
        "threshold_total_size_gb_gte": args.min_size_gb,
        "matches_count": len(matches),
        "data_subtrees_count": len(data_subtrees),
        "data_subtrees_matches_count": sum(item["matches_count"] for item in data_subtrees),
        "matches": matches,
        "data_subtrees": data_subtrees,
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=4, ensure_ascii=False) + "\n", encoding="utf-8")
    print(
        f"written {output_path} | matches_count={len(matches)} | data_subtrees_count={len(data_subtrees)} | "
        f"data_subtrees_matches_count={sum(item['matches_count'] for item in data_subtrees)}"
    )
    return 0


def extract_large_nodes(*, payload: dict[str, Any], min_size_gb: float) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    matches: list[dict[str, Any]] = []
    data_subtrees: list[dict[str, Any]] = []
    for node in payload.get("directory_tree", []):
        walk_tree(node=node, min_size_gb=min_size_gb, matches=matches, data_subtrees=data_subtrees)
    matches.sort(key=lambda item: (-item["total_size_gb"], item["path"]))
    data_subtrees = [item for item in data_subtrees if item["matches_count"] > 0]
    data_subtrees.sort(key=lambda item: (-item["matched_total_size_gb"], item["path"]))
    for subtree in data_subtrees:
        subtree["matches"].sort(key=lambda item: (-item["total_size_gb"], item["path"]))
    return matches, data_subtrees


def walk_tree(
    *,
    node: dict[str, Any],
    min_size_gb: float,
    matches: list[dict[str, Any]],
    data_subtrees: list[dict[str, Any]],
) -> None:
    children = node.get("children") or []

    if node.get("name") == "Data":
        subtree_matches: list[dict[str, Any]] = []
        for child in children:
            collect_subtree_matches(node=child, min_size_gb=min_size_gb, matches=subtree_matches)
        matches.extend(subtree_matches)
        data_subtrees.append(
            {
                "path": node.get("path"),
                "name": node.get("name"),
                "subtree_objects_count": node.get("objects_count", 0),
                "subtree_total_size_bytes": node.get("total_size_bytes", 0),
                "subtree_total_size_gb": float(node.get("total_size_gb", 0.0) or 0.0),
                "matches_count": len(subtree_matches),
                "matched_total_size_bytes": sum(int(item["total_size_bytes"]) for item in subtree_matches),
                "matched_total_size_gb": round(sum(float(item["total_size_gb"]) for item in subtree_matches), 3),
                "matches": subtree_matches,
            }
        )

    for child in children:
        walk_tree(node=child, min_size_gb=min_size_gb, matches=matches, data_subtrees=data_subtrees)


def collect_subtree_matches(
    *,
    node: dict[str, Any],
    min_size_gb: float,
    matches: list[dict[str, Any]],
) -> None:
    total_size_gb = float(node.get("total_size_gb", 0.0) or 0.0)
    children = node.get("children") or []
    if total_size_gb >= min_size_gb:
        matches.append(
            {
                "path": node.get("path"),
                "name": node.get("name"),
                "objects_count": node.get("objects_count", 0),
                "total_size_bytes": node.get("total_size_bytes", 0),
                "total_size_gb": total_size_gb,
                "children_count": len(children),
            }
        )
    for child in children:
        collect_subtree_matches(node=child, min_size_gb=min_size_gb, matches=matches)


if __name__ == "__main__":
    raise SystemExit(main())
