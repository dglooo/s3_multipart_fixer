from __future__ import annotations

from pathlib import Path
import argparse
import json
import sys

if __package__ in {None, ""}:
    sys.path.append(str(Path(__file__).resolve().parent.parent))

from s3_uploader.report import ReportRepository


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Show cumulative S3 upload/delete report.")
    parser.add_argument(
        "--results-dir",
        default=str(Path(__file__).resolve().parent / "results"),
        help="Path to directory with s3_upload_report.json",
    )
    parser.add_argument(
        "--bucket",
        help="Filter human-readable output to a specific bucket.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Number of recent operations to show in human-readable mode.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print raw JSON instead of formatted text.",
    )
    return parser


def main() -> None:
    parser = build_argument_parser()
    args = parser.parse_args()
    repository = ReportRepository(results_dir=Path(args.results_dir))

    if args.json:
        print(json.dumps(repository.load(), indent=2, ensure_ascii=False))
        return

    print(repository.render_human_summary(bucket=args.bucket, recent_limit=args.limit))


if __name__ == "__main__":
    main()
