"""Generated documents with known content for extraction accuracy testing.

Documents are built in memory so the expected text is exact and no binary
fixtures need to be committed. Requires reportlab and python-docx
(requirements-dev.txt).
"""
from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ExtractionCase:
    name: str
    filename: str
    content: bytes
    paragraphs: tuple[str, ...]
    page_of: dict[str, int] = field(default_factory=dict)
    table_cells: tuple[str, ...] = ()
    tables: int = 0
    scanned_pages: tuple[int, ...] = ()
    min_word_recall: float = 0.98
    max_wer: float = 0.05
    min_order: float = 1.0
    known_issue: str | None = None


@dataclass(frozen=True)
class InvalidCase:
    name: str
    filename: str
    content: bytes
    status_code: int
    needs_docling: bool = False


REPORT = (
    "Quarterly Report",
    "Revenue grew by 12 percent in the third quarter while operating costs fell slightly.",
    "The board approved a hiring plan for forty engineers across two regional offices.",
)
MANUAL = (
    "Safety Manual",
    "Press the red emergency button to stop the conveyor immediately.",
    "Maintenance staff must inspect the guard rails every Monday morning.",
)
PAGES = (
    "Installation begins by mounting the bracket on a flat wall surface.",
    "Connect the power cable only after the bracket screws are tightened.",
    "To reset the device hold the power button for ten seconds.",
)
COLUMN_LEFT = (
    "Press the red emergency button to stop the conveyor immediately. The button is located on "
    "the left side of the control panel and is clearly marked in red. Operators must be trained "
    "before using the conveyor system on any shift.",
    "Maintenance staff must inspect the guard rails every Monday morning and record any damage "
    "in the logbook kept beside the main entrance.",
)
COLUMN_RIGHT = (
    "Installation begins by mounting the bracket on a flat wall surface using the four screws "
    "supplied in the box.",
    "Connect the power cable only after the bracket screws are tightened. Never connect power "
    "while the cover is open, and always check that the fuse rating matches the label on the "
    "rear panel of the unit.",
)
TABLE = (("Region", "Sales", "Target"), ("North", "120", "100"), ("South", "95", "110"))
TABLE_CELLS = tuple(cell for row in TABLE for cell in row)


def build_corpus() -> list[ExtractionCase]:
    markdown_chars = (
        "The column invoice_total maps to field_name in the export.",
        "# this line starts with a hash but is not a heading",
        "Multiply 5 * 3 to get 15 and use a | b as the separator.",
    )
    unicode_text = (
        "Café owners in Zürich paid the naïve fee.",
        "Arabic greeting: مرحبا بالعالم",
        "Smart quotes “like these” and an em dash — stay intact.",
    )
    return [
        ExtractionCase("txt_simple", "simple.txt", _text(REPORT), REPORT),
        ExtractionCase("txt_markdown_chars", "markdown_chars.txt", _text(markdown_chars), markdown_chars),
        ExtractionCase("txt_utf8_bom_unicode", "unicode.txt",
                       b"\xef\xbb\xbf" + _text(unicode_text), unicode_text),
        ExtractionCase("txt_cp1252", "legacy.txt",
                       "\r\n\r\n".join(unicode_text[:1]).encode("cp1252"), unicode_text[:1]),
        ExtractionCase("txt_utf16", "utf16.txt", "\n\n".join(unicode_text).encode("utf-16"), unicode_text),
        ExtractionCase("docx_headings", "manual.docx", _docx(MANUAL), MANUAL),
        ExtractionCase("docx_table", "table.docx", _docx(REPORT[:2], table=TABLE),
                       REPORT[:2], table_cells=TABLE_CELLS, tables=1),
        ExtractionCase("docx_unicode", "unicode.docx", _docx(unicode_text), unicode_text),
        ExtractionCase("pdf_simple", "report.pdf", _pdf([REPORT]), REPORT),
        ExtractionCase("pdf_multipage", "pages.pdf", _pdf([[text] for text in PAGES]), PAGES,
                       page_of={text: page for page, text in enumerate(PAGES, start=1)}),
        ExtractionCase("pdf_grid_table", "grid_table.pdf", _pdf([REPORT[:2]], table=TABLE, grid=True),
                       REPORT[:2], table_cells=TABLE_CELLS, tables=1),
        ExtractionCase("pdf_borderless_table", "borderless_table.pdf",
                       _pdf([REPORT[:2]], table=TABLE, grid=False),
                       REPORT[:2], table_cells=TABLE_CELLS, tables=1,
                       known_issue="Tables without ruling lines may be flattened into body text."),
        ExtractionCase("pdf_two_columns", "two_columns.pdf", _two_column_pdf(COLUMN_LEFT, COLUMN_RIGHT),
                       COLUMN_LEFT + COLUMN_RIGHT),
        ExtractionCase("pdf_side_by_side_lines", "side_by_side.pdf", _two_column_pdf(MANUAL[1:], PAGES[:2]),
                       MANUAL[1:] + PAGES[:2],
                       known_issue="Short blocks at the same height in two columns are merged into one line."),
        ExtractionCase("pdf_scanned", "scanned.pdf",
                       _scanned_pdf("Invoice number 4471", "Total due 250 dollars"),
                       ("Invoice number 4471", "Total due 250 dollars"),
                       scanned_pages=(1,), min_word_recall=0.9, max_wer=0.2),
    ]


def build_invalid_cases() -> list[InvalidCase]:
    return [
        InvalidCase("unsupported_extension", "slides.pptx", b"PK\x03\x04fake", 415),
        InvalidCase("no_extension", "README", b"hello", 415),
        InvalidCase("legacy_doc", "old.doc", b"\xd0\xcf\x11\xe0legacy", 415),
        InvalidCase("pdf_extension_text_content", "fake.pdf", b"just some text", 415),
        InvalidCase("docx_extension_not_zip", "fake.docx", b"just some text", 415),
        InvalidCase("docx_zip_without_document", "fake.docx", _zip({"hello.txt": b"hi"}), 415),
        InvalidCase("txt_with_binary", "binary.txt", b"MZ\x90\x00\x03\x00\x00\x00", 415),
        InvalidCase("whitespace_only_txt", "blank.txt", b"  \r\n\t\n  ", 422),
        InvalidCase("corrupt_pdf", "corrupt.pdf", b"%PDF-1.4\n" + b"\x00garbage" * 50, 422, needs_docling=True),
        InvalidCase("encrypted_pdf", "encrypted.pdf", _pdf([REPORT], encrypt="secret"), 422, needs_docling=True),
        InvalidCase("blank_pdf", "blank.pdf", _pdf([[]]), 422, needs_docling=True),
        InvalidCase("corrupt_docx", "corrupt.docx",
                    _zip({"word/document.xml": b"<not-xml", "[Content_Types].xml": b"<bad"}), 422,
                    needs_docling=True),
    ]


def _text(paragraphs: tuple[str, ...]) -> bytes:
    return "\n\n".join(paragraphs).encode("utf-8")


def _zip(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def _docx(paragraphs: tuple[str, ...], table: tuple[tuple[str, ...], ...] = ()) -> bytes:
    from docx import Document

    document = Document()
    document.add_heading(paragraphs[0], level=1)
    for paragraph in paragraphs[1:]:
        document.add_paragraph(paragraph)
    if table:
        word_table = document.add_table(rows=len(table), cols=len(table[0]))
        for row_index, row in enumerate(table):
            for column_index, value in enumerate(row):
                word_table.cell(row_index, column_index).text = value
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _pdf(pages: list, table: tuple = (), grid: bool = True, encrypt: str | None = None) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    styles = getSampleStyleSheet()
    buffer = io.BytesIO()
    template = SimpleDocTemplate(buffer, pagesize=A4, encrypt=encrypt)
    story: list = []
    for page_number, paragraphs in enumerate(pages):
        if page_number:
            story.append(PageBreak())
        for index, text in enumerate(paragraphs):
            style = styles["Heading1"] if index == 0 and len(paragraphs) > 1 else styles["BodyText"]
            story.extend([Paragraph(text, style), Spacer(1, 12)])
        if not paragraphs:
            story.append(Spacer(1, 12))
    if table:
        pdf_table = Table([list(row) for row in table])
        if grid:
            pdf_table.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.black)]))
        story.append(pdf_table)
    template.build(story)
    return buffer.getvalue()


def _two_column_pdf(left: tuple[str, ...], right: tuple[str, ...]) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import BaseDocTemplate, Frame, FrameBreak, PageTemplate, Paragraph, Spacer

    styles = getSampleStyleSheet()
    buffer = io.BytesIO()
    width, height = A4
    margin, gap = 54, 36
    column = (width - 2 * margin - gap) / 2
    frames = [
        Frame(margin, margin, column, height - 2 * margin),
        Frame(margin + column + gap, margin, column, height - 2 * margin),
    ]
    template = BaseDocTemplate(buffer, pagesize=A4, pageTemplates=[PageTemplate(frames=frames)])
    story: list = []
    for text in left:
        story.extend([Paragraph(text, styles["BodyText"]), Spacer(1, 12)])
    story.append(FrameBreak())
    for text in right:
        story.extend([Paragraph(text, styles["BodyText"]), Spacer(1, 12)])
    template.build(story)
    return buffer.getvalue()


def _scanned_pdf(*lines: str) -> bytes:
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGB", (1240, 1754), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=48)
    for index, line in enumerate(lines):
        draw.text((120, 160 + index * 110), line, fill="black", font=font)
    buffer = io.BytesIO()
    image.save(buffer, format="PDF", resolution=150)
    return buffer.getvalue()
