"""Write the Milestone 1 test documents to a folder for manual testing in Swagger UI.

    python -m evaluation.export_test_files docs/evidence/milestone1/swagger_test_files

Writes one file per test case, named '<test id>_<filename>', plus 'manifest.json'
with the expected HTTP status and a verification query for each file.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from evaluation.corpus import build_corpus, build_invalid_cases

OVERSIZED_BYTES = 50 * 1024 * 1024 + 1


def test_cases() -> list[dict]:
    cases = []
    for case in build_corpus():
        cases.append({
            "group": "Valid upload", "name": case.name, "filename": case.filename,
            "content": case.content, "expected_status": 201,
            "query": " ".join(case.paragraphs[0].split()[:6]),
            "known_issue": case.known_issue,
        })
    for case in build_invalid_cases():
        group = "Unsupported or mismatched file" if case.status_code == 415 else "Unreadable document"
        cases.append({
            "group": group, "name": case.name, "filename": case.filename,
            "content": case.content, "expected_status": case.status_code,
        })
    cases.append({"group": "Size limit", "name": "empty_file", "filename": "empty.txt",
                  "content": b"", "expected_status": 400})
    cases.append({"group": "Size limit", "name": "oversized_file", "filename": "oversized.txt",
                  "content": None, "expected_status": 413})
    for number, case in enumerate(cases, start=1):
        case["id"] = f"TC{number:02d}"
        case["upload_name"] = f"{case['id']}_{case['filename']}"
    return cases


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("output", type=Path)
    args = parser.parse_args(argv)
    args.output.mkdir(parents=True, exist_ok=True)
    manifest = []
    for case in test_cases():
        path = args.output / case["upload_name"]
        if case["content"] is None:
            with path.open("wb") as handle:
                handle.truncate(OVERSIZED_BYTES)
        else:
            path.write_bytes(case["content"])
        manifest.append({key: value for key, value in case.items() if key != "content"})
    (args.output / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"Wrote {len(manifest)} test files to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
