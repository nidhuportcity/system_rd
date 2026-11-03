"""Extraction accuracy metrics computed on word tokens.

Tokens are casefolded word characters, so Markdown markup added by the parser
(table pipes, heading hashes) does not count as an error, while altered words
(for example 'invoice\\_total' instead of 'invoice_total') do.
"""
from __future__ import annotations

import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from difflib import SequenceMatcher

_ANCHOR_TOKENS = 8


def tokens(text: str) -> list[str]:
    return re.findall(r"\w+", unicodedata.normalize("NFKC", text).casefold())


def word_recall(expected: str, extracted: str) -> float:
    """Share of expected words (with multiplicity) present in the extracted text."""
    reference = Counter(tokens(expected))
    total = sum(reference.values())
    if not total:
        return 1.0
    return sum((reference & Counter(tokens(extracted))).values()) / total


def word_error_rate(expected: str, extracted: str) -> float:
    """Word-level edit distance divided by the number of expected words."""
    reference, hypothesis = tokens(expected), tokens(extracted)
    if not reference:
        return 0.0 if not hypothesis else 1.0
    errors = 0
    matcher = SequenceMatcher(None, reference, hypothesis, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "replace":
            errors += max(i2 - i1, j2 - j1)
        elif tag == "delete":
            errors += i2 - i1
        elif tag == "insert":
            errors += j2 - j1
    return errors / len(reference)


def _find(paragraph: str, extracted_tokens: list[str]) -> int | None:
    anchor = tokens(paragraph)[:_ANCHOR_TOKENS]
    if not anchor:
        return None
    for start in range(len(extracted_tokens) - len(anchor) + 1):
        if extracted_tokens[start:start + len(anchor)] == anchor:
            return start
    return None


def reading_order(paragraphs: Iterable[str], extracted: str) -> float | None:
    """Share of consecutive found paragraphs that appear in the expected order."""
    extracted_tokens = tokens(extracted)
    positions = [p for p in (_find(text, extracted_tokens) for text in paragraphs) if p is not None]
    if len(positions) < 2:
        return None
    pairs = list(zip(positions, positions[1:]))
    return sum(1 for first, second in pairs if first < second) / len(pairs)


def page_accuracy(page_of: Mapping[str, int], pages: Mapping[int, str]) -> float | None:
    """Share of paragraphs found on their expected page."""
    if not page_of:
        return None
    hits = sum(
        1 for text, page in page_of.items()
        if _find(text, tokens(pages.get(page, ""))) is not None
    )
    return hits / len(page_of)


def table_cell_recall(cells: Iterable[str], extracted: str) -> float | None:
    cells = list(cells)
    if not cells:
        return None
    extracted_tokens = tokens(extracted)
    return sum(1 for cell in cells if _find(cell, extracted_tokens) is not None) / len(cells)


@dataclass(frozen=True)
class ExtractionScores:
    word_recall: float
    word_error_rate: float
    reading_order: float | None
    page_accuracy: float | None
    table_cell_recall: float | None


def score_extraction(
    paragraphs: Iterable[str],
    extracted: str,
    pages: Mapping[int, str] | None = None,
    page_of: Mapping[str, int] | None = None,
    table_cells: Iterable[str] = (),
) -> ExtractionScores:
    paragraphs = list(paragraphs)
    table_cells = list(table_cells)
    expected = "\n\n".join(paragraphs + table_cells)
    return ExtractionScores(
        word_recall=word_recall(expected, extracted),
        word_error_rate=word_error_rate(expected, extracted),
        reading_order=reading_order(paragraphs, extracted),
        page_accuracy=page_accuracy(page_of or {}, pages or {}),
        table_cell_recall=table_cell_recall(table_cells, extracted),
    )
