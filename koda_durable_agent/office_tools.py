"""
Office file tools — read and write Word, Excel, and PDF files.

Works with any files on the local filesystem, including files synced from
OneDrive, SharePoint, Google Drive, or Dropbox.
"""
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

logger = logging.getLogger("koda.office")


# ── Word (.docx) ──────────────────────────────────────────────────────────────

def read_word_document(path: str) -> str:
    """Read the text content of a Word document (.docx).

    Args:
        path: Full path to the .docx file.
    """
    try:
        from docx import Document
    except ImportError:
        return "python-docx is not installed. Run: pip install python-docx"
    try:
        doc = Document(path)
        paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
        if not paragraphs:
            return f"Document is empty or contains no readable text: {path}"
        return "\n\n".join(paragraphs)
    except Exception as e:
        return f"Could not read {path}: {e}"


def write_word_document(path: str, content: str, overwrite: bool = False) -> str:
    """Create a new Word document (.docx) with the given text content.

    Each blank-line-separated block becomes its own paragraph.
    Confirm with the user before calling this — it creates or overwrites a file.

    Args:
        path: Full path where the .docx file should be written.
        content: Text content. Separate paragraphs with blank lines.
        overwrite: Set True only if the user has confirmed overwriting an existing file.
    """
    try:
        from docx import Document
    except ImportError:
        return "python-docx is not installed. Run: pip install python-docx"
    p = Path(path)
    if p.exists() and not overwrite:
        return f"File already exists at {path}. Set overwrite=True to replace it (confirm with user first)."
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        doc = Document()
        for block in content.split("\n\n"):
            block = block.strip()
            if block:
                doc.add_paragraph(block)
        doc.save(path)
        return f"Word document saved: {path}"
    except Exception as e:
        return f"Could not write {path}: {e}"


# ── Excel (.xlsx) ─────────────────────────────────────────────────────────────

def read_excel_file(path: str, sheet: str = "", max_rows: int = 200) -> str:
    """Read data from an Excel spreadsheet (.xlsx or .xls).

    Returns a plain-text table of the data.

    Args:
        path: Full path to the Excel file.
        sheet: Sheet name to read (leave blank for the first sheet).
        max_rows: Maximum number of rows to return (default 200).
    """
    try:
        import openpyxl
    except ImportError:
        return "openpyxl is not installed. Run: pip install openpyxl"
    try:
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        ws = wb[sheet] if sheet and sheet in wb.sheetnames else wb.active
        rows = []
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            if i >= max_rows:
                rows.append(f"... (truncated at {max_rows} rows)")
                break
            cells = [str(c) if c is not None else "" for c in row]
            rows.append("\t".join(cells))
        wb.close()
        if not rows:
            return f"Sheet is empty: {path}"
        sheets_info = f"Sheets: {', '.join(wb.sheetnames)}\n" if len(wb.sheetnames) > 1 else ""
        return f"{sheets_info}Sheet: {ws.title}\n\n" + "\n".join(rows)
    except Exception as e:
        return f"Could not read {path}: {e}"


def write_excel_file(
    path: str,
    rows: List[List[Any]],
    sheet: str = "Sheet1",
    overwrite: bool = False,
) -> str:
    """Create a new Excel spreadsheet with the given rows of data.

    Confirm with the user before calling this — it creates or overwrites a file.

    Args:
        path: Full path where the .xlsx file should be written.
        rows: List of rows, each row is a list of cell values.
              Example: [["Name", "Amount"], ["Invoice 1", 500], ["Invoice 2", 750]]
        sheet: Sheet name (default 'Sheet1').
        overwrite: Set True only if the user has confirmed overwriting an existing file.
    """
    try:
        import openpyxl
    except ImportError:
        return "openpyxl is not installed. Run: pip install openpyxl"
    p = Path(path)
    if p.exists() and not overwrite:
        return f"File already exists at {path}. Set overwrite=True to replace it (confirm with user first)."
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = sheet
        for row in rows:
            ws.append(row)
        wb.save(path)
        return f"Excel file saved: {path}  ({len(rows)} rows)"
    except Exception as e:
        return f"Could not write {path}: {e}"


def append_excel_rows(path: str, rows: List[List[Any]], sheet: str = "") -> str:
    """Append rows to an existing Excel spreadsheet without overwriting it.

    Args:
        path: Full path to the .xlsx file.
        rows: Rows to append.
        sheet: Sheet name (leave blank for the first sheet).
    """
    try:
        import openpyxl
    except ImportError:
        return "openpyxl is not installed. Run: pip install openpyxl"
    p = Path(path)
    if not p.exists():
        return f"File not found: {path}. Use write_excel_file to create it first."
    try:
        wb = openpyxl.load_workbook(path)
        ws = wb[sheet] if sheet and sheet in wb.sheetnames else wb.active
        for row in rows:
            ws.append(row)
        wb.save(path)
        return f"Appended {len(rows)} row(s) to {path} (sheet: {ws.title})"
    except Exception as e:
        return f"Could not append to {path}: {e}"


# ── PDF ───────────────────────────────────────────────────────────────────────

def read_pdf(path: str, pages: str = "") -> str:
    """Read the text content of a PDF file.

    Args:
        path: Full path to the PDF file.
        pages: Page range to read, e.g. '1-5' or '3'. Leave blank for all pages (up to 50).
    """
    try:
        import pypdf
    except ImportError:
        return "pypdf is not installed. Run: pip install pypdf"
    try:
        reader = pypdf.PdfReader(path)
        total = len(reader.pages)

        if pages:
            parts = pages.split("-")
            start = int(parts[0]) - 1
            end = int(parts[1]) if len(parts) > 1 else start + 1
        else:
            start = 0
            end = min(total, 50)

        texts = []
        for i in range(start, min(end, total)):
            text = reader.pages[i].extract_text() or ""
            if text.strip():
                texts.append(f"--- Page {i + 1} ---\n{text.strip()}")

        if not texts:
            return f"No readable text found in {path} (pages {start+1}-{end}). It may be a scanned image PDF."

        result = "\n\n".join(texts)
        if end < total:
            result += f"\n\n[Showing pages {start+1}-{end} of {total}. Specify pages='X-Y' to read more.]"
        return result
    except Exception as e:
        return f"Could not read {path}: {e}"
