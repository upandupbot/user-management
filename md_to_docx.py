"""Convert PIPES.md to PIPES.docx using python-docx.

Lightweight markdown converter — handles headings (# .. ###), GFM tables,
fenced code blocks (```), blockquotes (>), bullet lists, paragraphs, and
inline `code`. Tuned for the structure of PIPES.md, not a general markdown
renderer.

Usage:
    python md_to_docx.py PIPES.md PIPES.docx
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from docx import Document
from docx.enum.table import WD_ALIGN_VERTICAL
from docx.shared import Pt, RGBColor


CODE_FONT = "Consolas"
CODE_BG = RGBColor(0xF2, 0xF2, 0xF2)
INLINE_CODE_RE = re.compile(r"`([^`]+)`")


def _add_inline(paragraph, text: str) -> None:
    """Add a paragraph's text with inline `code` runs styled monospace."""
    parts = INLINE_CODE_RE.split(text)
    for i, part in enumerate(parts):
        if not part:
            continue
        run = paragraph.add_run(part)
        if i % 2 == 1:
            run.font.name = CODE_FONT


def _add_heading(doc: Document, level: int, text: str) -> None:
    h = doc.add_heading(level=min(level, 4))
    _add_inline(h, text)


def _add_paragraph(doc: Document, text: str, *, style: str | None = None) -> None:
    p = doc.add_paragraph(style=style) if style else doc.add_paragraph()
    _add_inline(p, text)


def _add_blockquote(doc: Document, lines: list[str]) -> None:
    text = " ".join(line.lstrip("> ").rstrip() for line in lines)
    p = doc.add_paragraph(style="Intense Quote")
    _add_inline(p, text)


def _add_code_block(doc: Document, lines: list[str]) -> None:
    text = "\n".join(lines)
    p = doc.add_paragraph()
    run = p.add_run(text)
    run.font.name = CODE_FONT
    run.font.size = Pt(9)


def _add_table(doc: Document, header: list[str], rows: list[list[str]]) -> None:
    cols = len(header)
    table = doc.add_table(rows=1 + len(rows), cols=cols)
    table.style = "Light Grid Accent 1"
    hdr_cells = table.rows[0].cells
    for i, h in enumerate(header):
        hdr_cells[i].text = ""
        _add_inline(hdr_cells[i].paragraphs[0], h.strip())
        for run in hdr_cells[i].paragraphs[0].runs:
            run.bold = True
    for r, row in enumerate(rows, start=1):
        cells = table.rows[r].cells
        for i in range(cols):
            cells[i].text = ""
            value = row[i].strip() if i < len(row) else ""
            _add_inline(cells[i].paragraphs[0], value)
            cells[i].vertical_alignment = WD_ALIGN_VERTICAL.TOP


def _split_table_row(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [cell.strip() for cell in line.split("|")]


def convert(md_path: Path, docx_path: Path) -> None:
    doc = Document()
    # Tighten body font.
    style = doc.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(11)

    lines = md_path.read_text(encoding="utf-8").splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Fenced code block
        if stripped.startswith("```"):
            i += 1
            buf: list[str] = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                buf.append(lines[i])
                i += 1
            _add_code_block(doc, buf)
            i += 1  # skip closing fence
            continue

        # Heading
        m = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if m:
            level = len(m.group(1))
            _add_heading(doc, level, m.group(2))
            i += 1
            continue

        # Horizontal rule
        if re.match(r"^-{3,}$", stripped):
            doc.add_paragraph().add_run("").add_break()
            i += 1
            continue

        # Blockquote (consume contiguous > lines)
        if stripped.startswith(">"):
            buf = []
            while i < len(lines) and lines[i].lstrip().startswith(">"):
                buf.append(lines[i])
                i += 1
            _add_blockquote(doc, buf)
            continue

        # Table: header line followed by separator line of |---|
        if stripped.startswith("|") and i + 1 < len(lines) and re.match(r"^\|?\s*[:\-\s|]+\|?\s*$", lines[i + 1].strip()) and "-" in lines[i + 1]:
            header = _split_table_row(stripped)
            i += 2  # skip header + separator
            rows: list[list[str]] = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(_split_table_row(lines[i]))
                i += 1
            _add_table(doc, header, rows)
            continue

        # Bullet list item
        if re.match(r"^[-*]\s+", stripped):
            text = re.sub(r"^[-*]\s+", "", stripped)
            _add_paragraph(doc, text, style="List Bullet")
            i += 1
            continue

        # Numbered list item
        if re.match(r"^\d+\.\s+", stripped):
            text = re.sub(r"^\d+\.\s+", "", stripped)
            _add_paragraph(doc, text, style="List Number")
            i += 1
            continue

        # Blank line
        if not stripped:
            i += 1
            continue

        # Plain paragraph
        _add_paragraph(doc, stripped)
        i += 1

    doc.save(str(docx_path))


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("Usage: python md_to_docx.py <input.md> <output.docx>", file=sys.stderr)
        return 2
    convert(Path(argv[1]), Path(argv[2]))
    print(f"Wrote {argv[2]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
