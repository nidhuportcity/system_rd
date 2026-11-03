import pytest

from evaluation.metrics import (
    page_accuracy,
    reading_order,
    score_extraction,
    table_cell_recall,
    tokens,
    word_error_rate,
    word_recall,
)


def test_tokens_ignore_markup_but_keep_word_characters():
    assert tokens("## Title\n| Région | invoice_total |") == ["title", "région", "invoice_total"]
    assert tokens(r"invoice\_total") == ["invoice", "_total"]


def test_word_recall_counts_repeated_words():
    assert word_recall("the cat and the dog", "the cat and dog") == pytest.approx(0.8)
    assert word_recall("", "anything") == 1.0


def test_word_error_rate():
    assert word_error_rate("a b c d", "a b c d") == 0.0
    assert word_error_rate("a b c d", "a x c d") == pytest.approx(0.25)
    assert word_error_rate("a b c d", "a b c d e f") == pytest.approx(0.5)
    assert word_error_rate("a b c d", "c d a b") == pytest.approx(1.0)


def test_reading_order():
    paragraphs = ["first paragraph here", "second paragraph here", "third paragraph here"]
    assert reading_order(paragraphs, " ".join(paragraphs)) == 1.0
    swapped = "second paragraph here first paragraph here third paragraph here"
    assert reading_order(paragraphs, swapped) == pytest.approx(0.5)
    assert reading_order(paragraphs, "nothing matches") is None


def test_page_accuracy():
    pages = {1: "Intro text on page one", 2: "Reset steps on page two"}
    assert page_accuracy({"Intro text": 1, "Reset steps": 2}, pages) == 1.0
    assert page_accuracy({"Intro text": 2, "Reset steps": 2}, pages) == 0.5
    assert page_accuracy({}, pages) is None


def test_table_cell_recall():
    table = "| Region | Sales |\n|---|---|\n| North | 120 |"
    assert table_cell_recall(["Region", "Sales", "North", "120"], table) == 1.0
    assert table_cell_recall(["North", "95"], table) == 0.5
    assert table_cell_recall([], table) is None


def test_score_extraction_combines_metrics():
    scores = score_extraction(["Hello world"], "## Hello world\n\n| A |", table_cells=["A"])
    assert scores.word_recall == 1.0
    assert scores.word_error_rate == 0.0
    assert scores.table_cell_recall == 1.0
    assert scores.page_accuracy is None
