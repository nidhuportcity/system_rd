from __future__ import annotations

import io
import logging
import re
import unicodedata
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {".pdf": "pdf", ".docx": "docx", ".txt": "txt", ".md": "md"}
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class DocumentError(Exception):
    """A client-side problem with an uploaded document."""

    status_code = 422


class UnsupportedDocumentError(DocumentError):
    status_code = 415


class DocumentParseError(DocumentError):
    status_code = 422


@dataclass(frozen=True)
class ParsedDocument:
    text: str
    file_type: str
    page_count: int | None = None
    pages: dict[int, str] = field(default_factory=dict)
    table_count: int = 0
    scanned_pages: list[int] = field(default_factory=list)
    encoding: str | None = None
    warnings: list[str] = field(default_factory=list)


def detect_file_type(content: bytes, filename: str) -> str:
    """Return the file type, rejecting unknown extensions and mismatched content."""
    extension = Path(filename).suffix.lower()
    file_type = SUPPORTED_EXTENSIONS.get(extension)
    if file_type is None:
        supported = ", ".join(sorted(SUPPORTED_EXTENSIONS))
        raise UnsupportedDocumentError(
            f"Unsupported file type '{extension or 'none'}'. Supported types: {supported}."
        )
    if file_type == "pdf" and b"%PDF-" not in content[:1024]:
        raise UnsupportedDocumentError("The file has a .pdf extension but is not a PDF.")
    if file_type == "docx" and not _is_docx(content):
        raise UnsupportedDocumentError(
            "The file has a .docx extension but is not a Word document. "
            "Legacy .doc files must be saved as .docx."
        )
    if file_type in {"txt", "md"} and _looks_binary(content):
        raise UnsupportedDocumentError(f"The file has a {extension} extension but contains binary data.")
    return file_type


def _is_docx(content: bytes) -> bool:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            return "word/document.xml" in archive.namelist()
    except zipfile.BadZipFile:
        return False


def _looks_binary(content: bytes) -> bool:
    if content.startswith((b"\xff\xfe", b"\xfe\xff")):
        return False
    return b"\x00" in content[:8192]


def decode_text(content: bytes) -> tuple[str, str]:
    """Decode plain text, honouring BOMs and falling back to Windows-1252."""
    if content.startswith(b"\xef\xbb\xbf"):
        return content.decode("utf-8-sig"), "utf-8-sig"
    if content.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return content.decode("utf-16"), "utf-16"
        except UnicodeDecodeError as exc:
            raise DocumentParseError("The text file has a UTF-16 marker but invalid content.") from exc
    for encoding in ("utf-8", "cp1252"):
        try:
            return content.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return content.decode("latin-1"), "latin-1"


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFC", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROL_CHARS.sub("", text)
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


class DocumentParser:
    """Validate uploads and extract normalized text from PDF, DOCX, TXT and Markdown."""

    def __init__(self) -> None:
        self._converter: Any = None

    @property
    def converter(self) -> Any:
        if self._converter is None:
            from docling.document_converter import DocumentConverter

            self._converter = DocumentConverter()
        return self._converter

    def parse(self, content: bytes, filename: str) -> ParsedDocument:
        file_type = detect_file_type(content, filename)
        if file_type == "txt":
            parsed = self._parse_text(content)
        else:
            parsed = self._parse_with_docling(content, file_type)
        if not parsed.text:
            raise DocumentParseError(
                "No text could be extracted from the document. If it is a scanned "
                "image, check that it is legible."
            )
        for warning in parsed.warnings:
            logger.warning("%s: %s", filename, warning)
        return parsed

    def _parse_text(self, content: bytes) -> ParsedDocument:
        # Docling would treat .txt as Markdown, escaping underscores and turning
        # lines starting with '#' into headings, so plain text is decoded directly.
        text, encoding = decode_text(content)
        warnings = []
        if encoding in {"cp1252", "latin-1"}:
            warnings.append(f"The file is not valid UTF-8; it was decoded as {encoding}.")
        return ParsedDocument(
            text=normalize_text(text), file_type="txt", encoding=encoding, warnings=warnings
        )

    def _parse_with_docling(self, content: bytes, file_type: str) -> ParsedDocument:
        from docling.datamodel.base_models import ConversionStatus, DocumentStream
        from docling.exceptions import ConversionError
        from docling_core.types.doc import TableItem

        stream = DocumentStream(name=f"upload.{file_type}", stream=io.BytesIO(content))
        try:
            result = self.converter.convert(stream)
        except ConversionError as exc:
            logger.warning("Docling could not convert a %s upload: %s", file_type, exc)
            if file_type == "pdf" and _is_encrypted_pdf(content):
                raise DocumentParseError("The PDF is password-protected; upload an unlocked copy.") from exc
            raise DocumentParseError(
                f"The {file_type.upper()} file could not be parsed; it may be corrupt."
            ) from exc
        document = result.document
        warnings = []
        if result.status == ConversionStatus.PARTIAL_SUCCESS:
            warnings.append("Only part of the document could be converted.")

        page_text: dict[int, list[str]] = {}
        table_count = 0
        for item, _level in document.iterate_items():
            if isinstance(item, TableItem):
                table_count += 1
                item_text = item.export_to_markdown(doc=document)
            else:
                item_text = getattr(item, "text", "")
            for provenance in getattr(item, "prov", []):
                page_text.setdefault(provenance.page_no, []).append(item_text)

        scanned_pages = _pages_without_text_layer(content) if file_type == "pdf" else []
        if scanned_pages:
            warnings.append(
                f"Pages {scanned_pages} have no embedded text and were read with OCR; "
                "verify the extracted text."
            )
        text = document.export_to_markdown(
            escape_underscores=False, escape_html=False, image_placeholder=""
        )
        return ParsedDocument(
            text=normalize_text(text),
            file_type=file_type,
            page_count=len(document.pages) or None,
            pages={page: normalize_text("\n\n".join(parts)) for page, parts in sorted(page_text.items())},
            table_count=table_count,
            scanned_pages=scanned_pages,
            warnings=warnings,
        )


def _is_encrypted_pdf(content: bytes) -> bool:
    import pypdfium2 as pdfium

    try:
        pdfium.PdfDocument(content).close()
    except pdfium.PdfiumError as exc:
        return "password" in str(exc).lower()
    return False


def _pages_without_text_layer(content: bytes) -> list[int]:
    """Return 1-based numbers of PDF pages that have no embedded text."""
    try:
        import pypdfium2 as pdfium

        pdf = pdfium.PdfDocument(content)
    except Exception:
        logger.warning("Could not inspect the PDF text layer.", exc_info=True)
        return []
    try:
        pages = []
        for index in range(len(pdf)):
            page = pdf[index]
            textpage = page.get_textpage()
            try:
                if not textpage.get_text_range().strip():
                    pages.append(index + 1)
            finally:
                textpage.close()
                page.close()
        return pages
    finally:
        pdf.close()
