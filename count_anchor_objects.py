#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


DEFAULT_INPUT = "report/objects_.json"
DEFAULT_OUTPUT = "report/anchor_objects_count.json"
DEFAULT_ANCHOR_NAME = "Data"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Count aggregated objects_count/size for nodes matched by anchor name in a count-objects JSON tree.",
    )
    parser.add_argument("--input", default=DEFAULT_INPUT, help=f"Input JSON file. Default: {DEFAULT_INPUT}")
    parser.add_argument("--output", default=DEFAULT_OUTPUT, help=f"Output JSON file. Default: {DEFAULT_OUTPUT}")
    parser.add_argument(
        "--anchor-name",
        default=DEFAULT_ANCHOR_NAME,
        help=f"Node name to match in directory_tree. Default: {DEFAULT_ANCHOR_NAME}",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    input_path = Path(args.input)
    output_path = Path(args.output)
    if output_path.parent == Path("."):
        output_path = Path("report") / output_path

    payload = json.loads(input_path.read_text(encoding="utf-8"))
    anchor_subtrees = collect_anchor_leaf_nodes(payload=payload, anchor_name=args.anchor_name)
    all_leaf_nodes = [leaf for subtree in anchor_subtrees for leaf in subtree["leaf_nodes"]]

    result = {
        "input_file": str(input_path),
        "output_file": str(output_path),
        "anchor_name": args.anchor_name,
        "matched_anchors_count": len(anchor_subtrees),
        "leaf_nodes_count": len(all_leaf_nodes),
        "total_objects_count": sum(int(item["objects_count"]) for item in all_leaf_nodes),
        "total_size_bytes": sum(int(item["total_size_bytes"]) for item in all_leaf_nodes),
        "total_size_gb": round(sum(float(item["total_size_gb"]) for item in all_leaf_nodes), 3),
        "anchor_subtrees": anchor_subtrees,
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=4, ensure_ascii=False) + "\n", encoding="utf-8")
    print(
        f"written {output_path} | anchor_name={args.anchor_name} | matched_anchors_count={len(anchor_subtrees)} | "
        f"leaf_nodes_count={result['leaf_nodes_count']} | total_objects_count={result['total_objects_count']}"
    )
    return 0


def collect_anchor_leaf_nodes(*, payload: dict[str, Any], anchor_name: str) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for node in payload.get("directory_tree", []):
        _walk_tree(node=node, anchor_name=anchor_name, matches=matches)
    matches.sort(key=lambda item: (-int(item["subtree_total_objects_count"]), item["path"]))
    return matches


def _walk_tree(*, node: dict[str, Any], anchor_name: str, matches: list[dict[str, Any]]) -> None:
    children = node.get("children") or []
    if node.get("name") == anchor_name:
        leaf_nodes: list[dict[str, Any]] = []
        for child in children:
            _collect_leaf_nodes(node=child, matches=leaf_nodes)
        if not children:
            _collect_leaf_nodes(node=node, matches=leaf_nodes)
        matches.append(
            {
                "path": node.get("path"),
                "name": node.get("name"),
                "subtree_total_objects_count": sum(int(item["objects_count"]) for item in leaf_nodes),
                "subtree_total_size_bytes": sum(int(item["total_size_bytes"]) for item in leaf_nodes),
                "subtree_total_size_gb": round(sum(float(item["total_size_gb"]) for item in leaf_nodes), 3),
                "children_count": len(children),
                "leaf_nodes_count": len(leaf_nodes),
                "leaf_nodes": leaf_nodes,
            }
        )
    for child in children:
        _walk_tree(node=child, anchor_name=anchor_name, matches=matches)


def _collect_leaf_nodes(*, node: dict[str, Any], matches: list[dict[str, Any]]) -> None:
    children = node.get("children") or []
    if not children:
        matches.append(
            {
                "path": node.get("path"),
                "name": node.get("name"),
                "objects_count": int(node.get("objects_count", 0) or 0),
                "total_size_bytes": int(node.get("total_size_bytes", 0) or 0),
                "total_size_gb": float(node.get("total_size_gb", 0.0) or 0.0),
            }
        )
        return
    for child in children:
        _collect_leaf_nodes(node=child, matches=matches)


if __name__ == "__main__":
    raise SystemExit(main())
