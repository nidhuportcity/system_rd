"""Extraction accuracy report.

    python -m evaluation.extraction_report
    python -m evaluation.extraction_report --corpus path/to/docs --json report.json

With --corpus, every PDF/DOCX/TXT/MD file that has a sibling '<file>.expected.txt'
(for example 'manual.pdf.expected.txt') is scored against it. Separate expected
paragraphs with blank lines so reading order can be measured.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from app.documents.parser import SUPPORTED_EXTENSIONS, DocumentError, DocumentParser
from evaluation.corpus import ExtractionCase, build_corpus
from evaluation.metrics import ExtractionScores, score_extraction


@dataclass(frozen=True)
class CaseResult:
    name: str
    file_type: str | None
    status: str
    seconds: float
    page_count: int | None = None
    tables_found: int | None = None
    scanned_pages: list[int] | None = None
    scores: ExtractionScores | None = None
    failures: list[str] | None = None
    note: str | None = None


def evaluate_case(parser: DocumentParser, case: ExtractionCase) -> CaseResult:
    started = time.perf_counter()
    try:
        parsed = parser.parse(case.content, case.filename)
    except DocumentError as exc:
        return CaseResult(case.name, None, "ERROR", time.perf_counter() - started, note=str(exc))
    seconds = time.perf_counter() - started
    scores = score_extraction(case.paragraphs, parsed.text, parsed.pages, case.page_of, case.table_cells)
    failures = check_thresholds(case, scores, parsed.table_count, parsed.scanned_pages)
    if not failures:
        status = "PASS"
    elif case.known_issue:
        status = "KNOWN"
    else:
        status = "FAIL"
    return CaseResult(
        case.name, parsed.file_type, status, seconds, parsed.page_count, parsed.table_count,
        parsed.scanned_pages, scores, failures, case.known_issue if failures else None,
    )


def check_thresholds(
    case: ExtractionCase, scores: ExtractionScores, tables_found: int, scanned_pages: list[int]
) -> list[str]:
    failures = []
    if scores.word_recall < case.min_word_recall:
        failures.append(f"word recall {scores.word_recall:.3f} < {case.min_word_recall}")
    if scores.word_error_rate > case.max_wer:
        failures.append(f"WER {scores.word_error_rate:.3f} > {case.max_wer}")
    if scores.reading_order is not None and scores.reading_order < case.min_order:
        failures.append(f"reading order {scores.reading_order:.2f} < {case.min_order}")
    if scores.page_accuracy is not None and scores.page_accuracy < 1.0:
        failures.append(f"page accuracy {scores.page_accuracy:.2f} < 1.0")
    if scores.table_cell_recall is not None and scores.table_cell_recall < 1.0:
        failures.append(f"table cell recall {scores.table_cell_recall:.2f} < 1.0")
    if tables_found < case.tables:
        failures.append(f"found {tables_found} of {case.tables} tables")
    if tuple(scanned_pages) != case.scanned_pages:
        failures.append(f"scanned pages {scanned_pages} != {list(case.scanned_pages)}")
    return failures


def load_corpus(directory: Path) -> list[ExtractionCase]:
    cases = []
    for path in sorted(directory.rglob("*")):
        expected = path.with_name(path.name + ".expected.txt")
        if path.suffix.lower() not in SUPPORTED_EXTENSIONS or not expected.is_file():
            continue
        paragraphs = tuple(
            block.strip() for block in expected.read_text(encoding="utf-8").split("\n\n") if block.strip()
        )
        cases.append(ExtractionCase(
            str(path.relative_to(directory)), path.name, path.read_bytes(), paragraphs,
            min_order=0.9, max_wer=0.1, min_word_recall=0.95,
        ))
    return cases


def _fmt(value: float | None) -> str:
    return "-" if value is None else f"{value:.3f}"


def print_report(results: list[CaseResult]) -> None:
    header = ("case", "type", "status", "recall", "WER", "order", "pages", "cells", "tables", "ocr", "sec")
    rows = [header]
    for result in results:
        scores = result.scores
        rows.append((
            result.name, result.file_type or "-", result.status,
            _fmt(scores and scores.word_recall), _fmt(scores and scores.word_error_rate),
            _fmt(scores and scores.reading_order), _fmt(scores and scores.page_accuracy),
            _fmt(scores and scores.table_cell_recall),
            "-" if result.tables_found is None else str(result.tables_found),
            ",".join(map(str, result.scanned_pages or [])) or "-", f"{result.seconds:.1f}",
        ))
    widths = [max(len(row[i]) for row in rows) for i in range(len(header))]
    for row in rows:
        print("  ".join(cell.ljust(width) for cell, width in zip(row, widths)))
    print()
    for result in results:
        for failure in result.failures or []:
            print(f"{result.name}: {failure}")
        if result.note:
            print(f"{result.name}: {result.note}")
    counts = {status: sum(r.status == status for r in results) for status in ("PASS", "KNOWN", "FAIL", "ERROR")}
    print("\n" + ", ".join(f"{count} {status}" for status, count in counts.items()))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--corpus", type=Path, help="Directory of real documents with .expected.txt files.")
    parser.add_argument("--json", type=Path, help="Also write the results to this JSON file.")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    cases = load_corpus(args.corpus) if args.corpus else build_corpus()
    if not cases:
        print("No documents with matching .expected.txt files were found.")
        return 1
    document_parser = DocumentParser()
    results = [evaluate_case(document_parser, case) for case in cases]
    print_report(results)
    if args.json:
        args.json.write_text(json.dumps([asdict(r) for r in results], indent=2), encoding="utf-8")
    return 1 if any(r.status in {"FAIL", "ERROR"} for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
