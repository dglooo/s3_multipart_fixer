from __future__ import annotations

import csv
import json
import sys
from pathlib import Path
from typing import Any, Iterable


class ReportWriter:
    def write_row(self, payload: dict[str, Any]) -> None:
        raise NotImplementedError

    def flush(self) -> None:
        return None

    def close(self) -> None:
        return None


class JsonLinesReportWriter(ReportWriter):
    def __init__(self, destination: str, flush_every: int = 1000) -> None:
        self._stream = _open_destination(destination)
        self._flush_every = flush_every
        self._rows_written = 0

    def write_row(self, payload: dict[str, Any]) -> None:
        self._stream.write(json.dumps(payload, sort_keys=True))
        self._stream.write("\n")
        self._rows_written += 1
        if self._should_flush():
            self.flush()

    def flush(self) -> None:
        self._stream.flush()

    def close(self) -> None:
        self.flush()
        if self._stream is not sys.stdout:
            self._stream.close()

    def _should_flush(self) -> bool:
        return self._stream is not sys.stdout and self._flush_every > 0 and self._rows_written % self._flush_every == 0


class CsvReportWriter(ReportWriter):
    def __init__(self, destination: str, fieldnames: Iterable[str], flush_every: int = 1000) -> None:
        self._stream = _open_destination(destination)
        self._fieldnames = list(fieldnames)
        self._writer = csv.DictWriter(self._stream, fieldnames=self._fieldnames)
        self._flush_every = flush_every
        self._rows_written = 0
        self._writer.writeheader()
        self.flush()

    def write_row(self, payload: dict[str, Any]) -> None:
        row = {field: payload.get(field) for field in self._fieldnames}
        self._writer.writerow(row)
        self._rows_written += 1
        if self._should_flush():
            self.flush()

    def flush(self) -> None:
        self._stream.flush()

    def close(self) -> None:
        self.flush()
        if self._stream is not sys.stdout:
            self._stream.close()

    def _should_flush(self) -> bool:
        return self._stream is not sys.stdout and self._flush_every > 0 and self._rows_written % self._flush_every == 0


def build_report_writer(
    format_name: str,
    destination: str,
    fieldnames: Iterable[str],
    flush_every: int = 1000,
) -> ReportWriter:
    normalized = format_name.lower()
    if normalized == "jsonl":
        return JsonLinesReportWriter(destination, flush_every=flush_every)
    if normalized == "csv":
        return CsvReportWriter(destination, fieldnames, flush_every=flush_every)
    raise ValueError(f"Unsupported report format: {format_name}")


def _open_destination(destination: str):
    if destination == "-":
        return sys.stdout
    path = Path(report_destination(destination))
    path.parent.mkdir(parents=True, exist_ok=True)
    return path.open("w", encoding="utf-8", newline="")


def report_destination(destination: str) -> str:
    """Place a bare report filename in report/; keep explicit paths and stdout."""
    if destination == "-":
        return destination
    path = Path(destination)
    if not path.is_absolute() and path.parent == Path("."):
        path = Path("report") / path
    return str(path)
