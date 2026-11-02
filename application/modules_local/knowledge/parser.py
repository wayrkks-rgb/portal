"""올린 파일을 시트 · 행으로 푼다. Flask 에 기대지 않는다(떼어서 WAS 로 옮길 수 있게).

폐쇄망이라 새 패키지를 들일 수 없다. 그래서 이미 쓰는 openpyxl 과 표준 라이브러리만
쓴다.

- ``.xlsx`` / ``.xlsm``: 시트마다 머리글 행을 찾아 표로 읽는다.
- ``.csv``: UTF-8(BOM 포함) → CP949 순서로 읽어 본다. 엑셀에서 저장한 CSV 는 대개 CP949 다.
- ``.docx`` / ``.pptx``: zip 안의 XML 에서 글만 뽑아 문단(슬라이드) 단위로 나눈다.
- ``.txt`` / ``.md``: 빈 줄로 문단을 나눈다.
- 그 밖(``.pdf`` · ``.hwp`` 등): 파일만 보관한다. 본문 검색은 안 되고 제목 · 설명으로 찾는다.

문서의 문단은 시트 하나(``본문``)의 행으로 저장한다. 표와 같은 길로 검색되고, 나중에
LLM 을 붙이면 그대로 임베딩할 조각이 된다.
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Iterable
from xml.etree import ElementTree

TABLE_TYPES = {"xlsx", "xlsm", "csv"}
DOCUMENT_TYPES = {"docx", "pptx", "txt", "md"}
#: 본문은 못 읽지만 보관은 하는 형식.
ATTACHMENT_TYPES = {"pdf", "hwp", "hwpx", "doc", "ppt", "vsdx", "png", "jpg", "jpeg"}
ALLOWED_TYPES = TABLE_TYPES | DOCUMENT_TYPES | ATTACHMENT_TYPES

#: 한 번에 올리는 자료의 행 상한(시트 합). 넘는 행은 버리고 잘렸다고 표시한다.
MAX_ROWS = 300_000
#: 한 행에서 읽을 열 상한. 서식만 칠해진 빈 열이 수천 개인 시트가 있다.
MAX_COLUMNS = 300
#: 셀 하나에 담을 글자 수 상한.
MAX_CELL_CHARS = 4_000
#: 머리글을 찾을 때 훑는 앞쪽 행 수.
HEADER_SCAN_ROWS = 20
#: zip(docx · pptx) 안의 XML 한 개를 풀 때의 상한. 압축 폭탄을 막는다.
MAX_XML_BYTES = 30 * 1024 * 1024
#: 문서 문단 하나의 글자 수. 너무 길면 나눈다(검색 결과와 임베딩 조각 크기).
CHUNK_CHARS = 1_200

DOCUMENT_SHEET = "본문"


class ParseError(ValueError):
    """사용자에게 그대로 보여 줄 수 있는 읽기 실패."""


@dataclass
class ParsedSheet:
    name: str
    columns: list[str]
    rows: list[list[str]]
    header_row: int = 0          # 1부터. 0 이면 머리글 없음(문서)
    truncated: bool = False


@dataclass
class ParsedFile:
    file_type: str
    kind: str                    # TABLE / DOCUMENT / ATTACHMENT
    sheets: list[ParsedSheet] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def row_count(self) -> int:
        return sum(len(sheet.rows) for sheet in self.sheets)

    @property
    def searchable(self) -> bool:
        return self.kind != "ATTACHMENT"


def file_type_of(filename: str) -> str:
    return Path(str(filename or "")).suffix.lower().lstrip(".")


def kind_of(file_type: str) -> str:
    if file_type in TABLE_TYPES:
        return "TABLE"
    if file_type in DOCUMENT_TYPES:
        return "DOCUMENT"
    return "ATTACHMENT"


# ── 값 다듬기 ─────────────────────────────────────────────────────────────
def cell_text(value: Any) -> str:
    """셀 값을 화면 · 검색에 쓸 글자로 바꾼다. 엑셀이 붙인 ``.0`` 은 뗀다."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S") if (value.hour or value.minute or value.second) \
            else value.strftime("%Y-%m-%d")
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, float):
        if value != value:  # NaN
            return ""
        if value.is_integer() and abs(value) < 1e15:
            return str(int(value))
        return repr(value) if abs(value) < 1e-4 else f"{value:.10g}"
    text = str(value).replace("\r\n", "\n").replace("\r", "\n").strip()
    return text[:MAX_CELL_CHARS]


def _trim(values: list[str]) -> list[str]:
    end = len(values)
    while end and not values[end - 1]:
        end -= 1
    return values[:end]


def _unique_columns(raw: Iterable[str], width: int) -> list[str]:
    """빈 머리글은 ``열3`` 으로, 겹치는 이름은 ``이름(2)`` 로 바꾼다."""
    names: list[str] = []
    seen: dict[str, int] = {}
    raw = list(raw)
    for index in range(width):
        base = " ".join((raw[index] if index < len(raw) else "").split()) or f"열{index + 1}"
        count = seen.get(base, 0) + 1
        seen[base] = count
        names.append(base if count == 1 else f"{base}({count})")
    return names


def guess_header(rows: list[list[str]]) -> int:
    """앞쪽 행에서 머리글을 고른다. 0 부터 센 위치를 돌려준다.

    보고용 엑셀은 1~2행에 제목 · 작성일이 있고 그 아래에 머리글이 온다. 그래서 첫 행이
    아니라 **값이 충분히 찬 첫 행**을 머리글로 본다(가장 많이 찬 행의 절반 이상, 최소 2칸).
    """
    sample = rows[:HEADER_SCAN_ROWS]
    filled = [sum(1 for value in row if value) for row in sample]
    if not filled or max(filled) == 0:
        return 0
    need = max(2, (max(filled) + 1) // 2) if max(filled) >= 2 else 1
    for index, count in enumerate(filled):
        if count >= need:
            return index
    return 0


def _table(name: str, rows: list[list[str]], header_row: int | None, budget: int) -> ParsedSheet:
    rows = [_trim(row[:MAX_COLUMNS]) for row in rows]
    if header_row is None or header_row <= 0:
        head = guess_header(rows)
    else:
        head = min(header_row - 1, max(len(rows) - 1, 0))
    header = rows[head] if rows else []
    body = [row for row in rows[head + 1:] if any(row)]
    width = max([len(header)] + [len(row) for row in body]) if (header or body) else 0
    truncated = len(body) > budget
    body = body[:budget]
    columns = _unique_columns(header, width)
    padded = [row + [""] * (width - len(row)) for row in body]
    return ParsedSheet(name=name, columns=columns, rows=padded, header_row=head + 1 if rows else 0,
                       truncated=truncated)


# ── 형식별 읽기 ───────────────────────────────────────────────────────────
def _read_xlsx(path: Path, header_row: int | None) -> ParsedFile:
    from openpyxl import load_workbook

    try:
        workbook = load_workbook(path, read_only=True, data_only=True)
    except Exception as exc:  # 암호 걸린 파일 · 깨진 파일
        raise ParseError(f"엑셀을 열 수 없습니다. 암호가 걸렸거나 손상된 파일일 수 있습니다: {exc}") from exc
    parsed = ParsedFile(file_type="xlsx", kind="TABLE")
    budget = MAX_ROWS
    try:
        for sheet in workbook.worksheets:
            if getattr(sheet, "sheet_state", "visible") != "visible":
                parsed.warnings.append(f"숨긴 시트는 건너뜁니다: {sheet.title}")
                continue
            rows: list[list[str]] = []
            for values in sheet.iter_rows(values_only=True, max_col=MAX_COLUMNS):
                rows.append([cell_text(value) for value in values])
                # 머리글 위치는 모르므로 행 상한 + 머리글 탐색분까지만 읽는다.
                if len(rows) > budget + HEADER_SCAN_ROWS:
                    break
            if not any(any(row) for row in rows):
                continue
            table = _table(sheet.title, rows, header_row, budget)
            if not table.rows:
                parsed.warnings.append(f"머리글만 있고 값이 없는 시트입니다: {sheet.title}")
            parsed.sheets.append(table)
            budget -= len(table.rows)
            if table.truncated or budget <= 0:
                parsed.warnings.append(f"행이 {MAX_ROWS:,}개를 넘어 뒤쪽은 저장하지 않았습니다 ({sheet.title}).")
                break
    finally:
        workbook.close()
    if not parsed.sheets:
        raise ParseError("값이 있는 시트가 없습니다.")
    return parsed


def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp949"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _read_csv(path: Path, header_row: int | None) -> ParsedFile:
    text = _decode(path.read_bytes())
    try:
        dialect = csv.Sniffer().sniff(text[:8192], delimiters=",\t;|")
    except csv.Error:
        dialect = csv.excel
    rows = [[cell_text(value) for value in row] for row in csv.reader(io.StringIO(text), dialect)]
    if not any(any(row) for row in rows):
        raise ParseError("CSV 에 값이 없습니다.")
    table = _table(path.stem, rows[: MAX_ROWS + HEADER_SCAN_ROWS + 1], header_row, MAX_ROWS)
    parsed = ParsedFile(file_type="csv", kind="TABLE", sheets=[table])
    if table.truncated or len(rows) > MAX_ROWS + HEADER_SCAN_ROWS + 1:
        table.truncated = True
        parsed.warnings.append(f"행이 {MAX_ROWS:,}개를 넘어 뒤쪽은 저장하지 않았습니다.")
    return parsed


def _chunks(paragraphs: Iterable[str]) -> list[str]:
    """짧은 문단은 이어 붙이고 긴 문단은 자른다. 검색 결과 한 줄 = 읽을 만한 덩어리."""
    out: list[str] = []
    buffer = ""
    for paragraph in paragraphs:
        paragraph = " ".join(paragraph.split())
        if not paragraph:
            continue
        while len(paragraph) > CHUNK_CHARS:
            if buffer:
                out.append(buffer)
                buffer = ""
            cut = paragraph.rfind(" ", 0, CHUNK_CHARS)
            cut = cut if cut > CHUNK_CHARS // 2 else CHUNK_CHARS
            out.append(paragraph[:cut].strip())
            paragraph = paragraph[cut:].strip()
        if buffer and len(buffer) + len(paragraph) + 1 > CHUNK_CHARS // 3:
            out.append(buffer)
            buffer = paragraph
        else:
            buffer = f"{buffer} {paragraph}".strip()
    if buffer:
        out.append(buffer)
    return out


def _document(file_type: str, sections: list[tuple[str, list[str]]]) -> ParsedFile:
    """``[(위치, [문단...])]`` → 시트 하나(본문)의 행 ``[위치, 내용]``."""
    rows: list[list[str]] = []
    for place, paragraphs in sections:
        for chunk in _chunks(paragraphs):
            rows.append([place, chunk])
    parsed = ParsedFile(file_type=file_type, kind="DOCUMENT")
    if not rows:
        parsed.warnings.append("글자를 찾지 못했습니다. 스캔한 이미지 문서는 본문 검색이 안 됩니다.")
    truncated = len(rows) > MAX_ROWS
    parsed.sheets.append(ParsedSheet(name=DOCUMENT_SHEET, columns=["위치", "내용"],
                                     rows=rows[:MAX_ROWS], header_row=0, truncated=truncated))
    return parsed


def _zip_xml(archive: zipfile.ZipFile, name: str) -> ElementTree.Element:
    info = archive.getinfo(name)
    if info.file_size > MAX_XML_BYTES:
        raise ParseError(f"문서 안의 {name} 가 너무 큽니다({info.file_size:,} 바이트).")
    data = archive.read(name)
    if b"<!DOCTYPE" in data[:2048] or b"<!ENTITY" in data:
        raise ParseError("문서 안에 허용하지 않는 XML 선언(DOCTYPE)이 있습니다.")
    return ElementTree.fromstring(data)


_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"


def _open_zip(path: Path) -> zipfile.ZipFile:
    try:
        return zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise ParseError("문서를 열 수 없습니다. 예전 형식(.doc · .ppt)이면 docx · pptx 로 다시 저장해 주세요.") from exc


def _read_docx(path: Path) -> ParsedFile:
    with _open_zip(path) as archive:
        root = _zip_xml(archive, "word/document.xml")
    paragraphs = []
    for paragraph in root.iter(f"{_W}p"):
        text = "".join(node.text or "" for node in paragraph.iter() if node.tag in (f"{_W}t", f"{_W}tab"))
        paragraphs.append(text)
    return _document("docx", [("문서", paragraphs)])


def _slide_number(name: str) -> int:
    match = re.search(r"(\d+)\.xml$", name)
    return int(match.group(1)) if match else 0


def _read_pptx(path: Path) -> ParsedFile:
    sections: list[tuple[str, list[str]]] = []
    with _open_zip(path) as archive:
        slides = sorted(
            (name for name in archive.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)),
            key=_slide_number,
        )
        for name in slides:
            root = _zip_xml(archive, name)
            paragraphs = [
                "".join(node.text or "" for node in paragraph.iter(f"{_A}t"))
                for paragraph in root.iter(f"{_A}p")
            ]
            sections.append((f"슬라이드 {_slide_number(name)}", paragraphs))
    return _document("pptx", sections)


def _read_text(path: Path, file_type: str) -> ParsedFile:
    text = _decode(path.read_bytes())
    paragraphs = re.split(r"\n\s*\n", text.replace("\r\n", "\n"))
    return _document(file_type, [("문서", paragraphs)])


def parse_file(path: Path, filename: str, header_row: int | None = None) -> ParsedFile:
    """``filename`` 의 확장자로 형식을 정한다(``path`` 는 임시 저장 이름일 수 있다)."""
    file_type = file_type_of(filename)
    if file_type == "xls":
        raise ParseError("예전 엑셀 형식(.xls)은 읽을 수 없습니다. 엑셀에서 .xlsx 로 다시 저장해 올려 주세요.")
    if file_type not in ALLOWED_TYPES:
        allowed = ", ".join(sorted(ALLOWED_TYPES))
        raise ParseError(f"올릴 수 없는 형식입니다(.{file_type or '?'}). 가능한 형식: {allowed}")
    path = Path(path)
    if file_type in ("xlsx", "xlsm"):
        parsed = _read_xlsx(path, header_row)
        parsed.file_type = file_type
        return parsed
    if file_type == "csv":
        return _read_csv(path, header_row)
    if file_type == "docx":
        return _read_docx(path)
    if file_type == "pptx":
        return _read_pptx(path)
    if file_type in ("txt", "md"):
        return _read_text(path, file_type)
    return ParsedFile(
        file_type=file_type,
        kind="ATTACHMENT",
        warnings=[f".{file_type} 는 본문을 읽지 않고 파일만 보관합니다. 제목 · 설명 · 태그로 찾을 수 있습니다."],
    )
