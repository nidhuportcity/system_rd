import pytest

from app.documents.parser import (
    DocumentParseError,
    DocumentParser,
    UnsupportedDocumentError,
    decode_text,
    detect_file_type,
    normalize_text,
)
from evaluation.corpus import build_invalid_cases


class NoDoclingParser(DocumentParser):
    @property
    def converter(self):
        raise AssertionError("Plain text must not be sent to Docling")


VALIDATION_CASES = [case for case in build_invalid_cases() if not case.needs_docling]


@pytest.mark.parametrize("case", VALIDATION_CASES, ids=lambda case: case.name)
def test_invalid_uploads_are_rejected_without_docling(case):
    with pytest.raises((UnsupportedDocumentError, DocumentParseError)) as error:
        NoDoclingParser().parse(case.content, case.filename)
    assert error.value.status_code == case.status_code


@pytest.mark.parametrize(("filename", "content", "file_type"), [
    ("report.PDF", b"%PDF-1.7\n...", "pdf"),
    ("report.pdf", b"\n\n%PDF-1.4\n...", "pdf"),
    ("notes.txt", b"hello", "txt"),
    ("notes.md", b"# Title", "md"),
    ("utf16.txt", "hi".encode("utf-16"), "txt"),
])
def test_detect_file_type_accepts_supported_files(filename, content, file_type):
    assert detect_file_type(content, filename) == file_type


@pytest.mark.parametrize(("content", "text", "encoding"), [
    ("naïve".encode("utf-8"), "naïve", "utf-8"),
    (b"\xef\xbb\xbfna\xc3\xafve", "naïve", "utf-8-sig"),
    ("naïve".encode("utf-16"), "naïve", "utf-16"),
    ("naïve “quoted”".encode("cp1252"), "naïve “quoted”", "cp1252"),
    (b"\x81\x8d", "\x81\x8d", "latin-1"),
])
def test_decode_text(content, text, encoding):
    assert decode_text(content) == (text, encoding)


def test_normalize_text():
    raw = "Café\r\nline two   \r\rline\x00 three\x0c\n\n\n\n\tend  "
    assert normalize_text(raw) == "Café\nline two\n\nline three\n\n\tend"


def test_plain_text_is_not_rewritten_as_markdown():
    content = b"invoice_total = 5 * 3\n# not a heading\n| a | b |"
    parsed = NoDoclingParser().parse(content, "notes.txt")
    assert parsed.text == "invoice_total = 5 * 3\n# not a heading\n| a | b |"
    assert parsed.file_type == "txt"
    assert parsed.encoding == "utf-8"
    assert parsed.warnings == []


def test_legacy_encoding_produces_warning():
    parsed = NoDoclingParser().parse("Zürich".encode("cp1252"), "legacy.txt")
    assert parsed.text == "Zürich"
    assert parsed.warnings == ["The file is not valid UTF-8; it was decoded as cp1252."]
