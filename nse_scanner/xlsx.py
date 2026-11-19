"""A tiny XLSX writer — the audit's spreadsheet, without a dependency.

The workflow installs ``requirements.txt`` on every run and a spreadsheet is not
worth a new wheel there, so this writes the smallest file Excel, LibreOffice and
Google Sheets all open: one worksheet per sheet, inline strings and plain
numbers, no styles, no shared-string table, no compression tricks.

Anything richer (formulas, formats, a frozen header) is a job for a spreadsheet
the human builds from the JSON, which the audit also ships.
"""
from __future__ import annotations

import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

_SHEET = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<sheetData>{rows}</sheetData></worksheet>"""

_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
{sheets}</Types>"""

_SHEET_OVERRIDE = ('<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType='
                   '"application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>')

_ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""

_WORKBOOK = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<sheets>{sheets}</sheets></workbook>"""

_WORKBOOK_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
{rels}</Relationships>"""


def _column(index: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA."""
    out = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        out = chr(65 + rem) + out
    return out


def _cell(ref: str, value) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, bool):
        return f'<c r="{ref}" t="b"><v>{int(value)}</v></c>'
    if isinstance(value, (int, float)):
        if value != value or value in (float("inf"), float("-inf")):     # NaN/inf
            return f'<c r="{ref}" t="inlineStr"><is><t>—</t></is></c>'
        return f'<c r="{ref}"><v>{value}</v></c>'
    text = escape(str(value))
    return f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{text}</t></is></c>'


def _rows(rows: list[list]) -> str:
    parts = []
    for r, row in enumerate(rows, start=1):
        cells = "".join(_cell(f"{_column(c)}{r}", value) for c, value in enumerate(row))
        parts.append(f'<row r="{r}">{cells}</row>')
    return "".join(parts)


def write(path: str | Path, sheets: dict[str, list[list]]) -> Path:
    """Write ``{sheet name: rows}`` to ``path`` and return it.

    Sheet names are sanitised the way Excel insists: at most 31 characters, none
    of ``[]:*?/\\``, and no duplicates.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    used: set[str] = set()
    named: list[tuple[str, list[list]]] = []
    for name, rows in sheets.items():
        clean = "".join(ch for ch in str(name) if ch not in "[]:*?/\\")[:31] or "Sheet"
        base, n = clean, 2
        while clean in used:
            clean = f"{base[:28]}_{n}"
            n += 1
        used.add(clean)
        named.append((clean, rows or [[""]]))
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CONTENT_TYPES.format(
            sheets="".join(_SHEET_OVERRIDE.format(i=i)
                          for i in range(1, len(named) + 1))))
        z.writestr("_rels/.rels", _ROOT_RELS)
        z.writestr("xl/workbook.xml", _WORKBOOK.format(sheets="".join(
            f'<sheet name="{escape(name)}" sheetId="{i}" r:id="rId{i}"/>'
            for i, (name, _) in enumerate(named, start=1))))
        z.writestr("xl/_rels/workbook.xml.rels", _WORKBOOK_RELS.format(rels="".join(
            f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/'
            f'officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>'
            for i in range(1, len(named) + 1))))
        for i, (_, rows) in enumerate(named, start=1):
            z.writestr(f"xl/worksheets/sheet{i}.xml", _SHEET.format(rows=_rows(rows)))
    return path
