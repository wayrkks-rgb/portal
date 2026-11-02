"""지식 저장소: 올리기 → 찾기 → 열기 → 새 버전 → 지우기가 실제 앱에서 이어지는지.

파일 읽기(parser)는 따로, 나머지는 통합 웹을 띄워 HTTP 로 확인한다. 화면은 API 를
부르기만 하므로 API 가 맞으면 화면이 맞는다. 화면과 스크립트의 짝은 맨 아래에서 본다.
"""

from __future__ import annotations

import io
import re
import zipfile
from datetime import datetime
from pathlib import Path

import pytest
from openpyxl import Workbook

from application.modules_local.knowledge.parser import (
    MAX_COLUMNS,
    ParseError,
    cell_text,
    guess_header,
    parse_file,
)
from application.settings import BASE_DIR

# ── 파일 만들기 ───────────────────────────────────────────────────────────


def xlsx_bytes(sheets: dict[str, list[list]], hidden: tuple[str, ...] = ()) -> bytes:
    workbook = Workbook()
    workbook.remove(workbook.active)
    for name, rows in sheets.items():
        sheet = workbook.create_sheet(name)
        for row in rows:
            sheet.append(row)
        if name in hidden:
            sheet.sheet_state = "hidden"
    stream = io.BytesIO()
    workbook.save(stream)
    return stream.getvalue()


SERVERS = {
    "서버목록": [
        ["서버 자산 현황", None, None, None],          # 제목 행
        ["기준일: 2026-10-31", None, None, None],
        ["호스트명", "IP", "OS", "담당"],
        ["aixprd01", "10.1.1.1", "AIX 7.2", "김철수"],
        ["lnxweb02", "10.1.1.2", "RHEL 8.6", "이영희"],
        [None, None, None, None],                       # 빈 행
        ["aixdev03", "10.1.1.3", "AIX 7.3", "박민수"],
    ],
    "백업정책": [
        ["대상", "주기", "보관"],
        ["aixprd01", "매일", 30],
        ["lnxweb02", "매주", 4.0],
    ],
}


def docx_bytes(paragraphs: list[str]) -> bytes:
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    body = "".join(f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>" for text in paragraphs)
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("word/document.xml", f'<?xml version="1.0"?><w:document xmlns:w="{ns}"><w:body>{body}</w:body></w:document>')
    return stream.getvalue()


def pptx_bytes(slides: list[list[str]]) -> bytes:
    ns = "http://schemas.openxmlformats.org/drawingml/2006/main"
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for number, texts in enumerate(slides, start=1):
            paragraphs = "".join(f"<a:p><a:r><a:t>{text}</a:t></a:r></a:p>" for text in texts)
            archive.writestr(f"ppt/slides/slide{number}.xml", f'<p:sld xmlns:p="x" xmlns:a="{ns}">{paragraphs}</p:sld>')
    return stream.getvalue()


def write(tmp_path: Path, name: str, data: bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


# ── 파일 읽기 ─────────────────────────────────────────────────────────────


def test_title_rows_above_the_header_are_skipped(tmp_path) -> None:
    parsed = parse_file(write(tmp_path, "s.xlsx", xlsx_bytes(SERVERS)), "s.xlsx")
    servers, backup = parsed.sheets
    assert servers.header_row == 3
    assert servers.columns == ["호스트명", "IP", "OS", "담당"]
    assert [row[0] for row in servers.rows] == ["aixprd01", "lnxweb02", "aixdev03"]   # 빈 행은 빠진다
    assert backup.rows[1] == ["lnxweb02", "매주", "4"]                                  # 4.0 → 4
    assert parsed.row_count == 5 and parsed.kind == "TABLE"


def test_header_row_can_be_given(tmp_path) -> None:
    parsed = parse_file(write(tmp_path, "s.xlsx", xlsx_bytes(SERVERS)), "s.xlsx", header_row=4)
    assert parsed.sheets[0].columns[0] == "aixprd01"


def test_blank_and_duplicate_headers_get_names() -> None:
    from application.modules_local.knowledge.parser import _unique_columns

    assert _unique_columns(["이름", "", "이름", "  a  b "], 5) == ["이름", "열2", "이름(2)", "a b", "열5"]


def test_hidden_sheets_are_skipped_with_a_note(tmp_path) -> None:
    data = xlsx_bytes({"보임": [["a", "b"], [1, 2]], "숨김": [["x", "y"], [3, 4]]}, hidden=("숨김",))
    parsed = parse_file(write(tmp_path, "h.xlsx", data), "h.xlsx")
    assert [sheet.name for sheet in parsed.sheets] == ["보임"]
    assert any("숨김" in warning for warning in parsed.warnings)


def test_cp949_csv_is_read(tmp_path) -> None:
    text = "호스트,담당\r\naixprd01,김철수\r\n"
    parsed = parse_file(write(tmp_path, "c.csv", text.encode("cp949")), "목록.csv")
    assert parsed.sheets[0].columns == ["호스트", "담당"]
    assert parsed.sheets[0].rows == [["aixprd01", "김철수"]]


def test_word_and_powerpoint_become_paragraph_rows(tmp_path) -> None:
    doc = parse_file(write(tmp_path, "d.docx", docx_bytes(["백업 절차", "1. 매일 02시 전체 백업을 확인한다."])), "d.docx")
    assert doc.kind == "DOCUMENT" and doc.sheets[0].columns == ["위치", "내용"]
    assert "02시 전체 백업" in " ".join(row[1] for row in doc.sheets[0].rows)

    ppt = parse_file(write(tmp_path, "p.pptx", pptx_bytes([["표지"], ["장애 대응", "DR 전환 순서"]])), "p.pptx")
    assert [row[0] for row in ppt.sheets[0].rows] == ["슬라이드 1", "슬라이드 2"]


def test_xml_with_a_doctype_is_refused(tmp_path) -> None:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("word/document.xml", '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><x/>')
    with pytest.raises(ParseError, match="DOCTYPE"):
        parse_file(write(tmp_path, "bad.docx", stream.getvalue()), "bad.docx")


def test_old_and_unknown_formats_are_explained(tmp_path) -> None:
    with pytest.raises(ParseError, match=".xlsx 로 다시 저장"):
        parse_file(write(tmp_path, "a.xls", b"x"), "a.xls")
    with pytest.raises(ParseError, match="올릴 수 없는 형식"):
        parse_file(write(tmp_path, "a.exe", b"x"), "a.exe")
    pdf = parse_file(write(tmp_path, "a.pdf", b"%PDF-1.4"), "a.pdf")
    assert pdf.kind == "ATTACHMENT" and not pdf.searchable and not pdf.sheets


def test_cell_text_and_header_guess() -> None:
    assert cell_text(datetime(2026, 10, 31)) == "2026-10-31"
    assert cell_text(3.0) == "3" and cell_text(None) == "" and cell_text(True) == "TRUE"
    assert guess_header([["제목", "", ""], ["a", "b", "c"], ["1", "2", "3"]]) == 1
    assert MAX_COLUMNS >= 100


# ── 앱에서 ────────────────────────────────────────────────────────────────


@pytest.fixture()
def portal(tmp_path, monkeypatch):
    # 실제 대메뉴 정의 파일을 그대로 쓴다. 설정이 빠지면 메뉴도 API 도 없어야 하므로.
    (tmp_path / "config" / "modules").mkdir(parents=True)
    (tmp_path / "config" / "modules" / "knowledge.yaml").write_text(
        (BASE_DIR / "config" / "modules" / "knowledge.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setenv("ASSET_APP_ROOT", str(tmp_path))
    monkeypatch.setenv("FLASK_SECRET_KEY", "test-secret")
    monkeypatch.chdir(tmp_path)
    from application import create_app
    from application.db import reset_database_manager
    from application.modules.panels import clear_local_panels

    reset_database_manager()
    app = create_app()
    yield app
    reset_database_manager()
    clear_local_panels()


def login(app, username="hong", role="user", name="홍길동"):
    client = app.test_client()
    with client.session_transaction() as session:
        session["user"] = {"id": 1, "username": username, "role": role, "name": name}
    return client


def upload(client, data: bytes, filename: str, **form):
    body = {"file": (io.BytesIO(data), filename), **form}
    return client.post("/api/knowledge/datasets", data=body, content_type="multipart/form-data")


def test_upload_search_open_and_page(portal, tmp_path) -> None:
    client = login(portal)
    res = upload(client, xlsx_bytes(SERVERS), "서버현황.xlsx", title="서버 현황", category="서버 자산",
                 tags="AIX, #백업; AIX", source_note="ITSM 추출")
    body = res.get_json()
    assert res.status_code == 200, body
    assert (body["version_no"], body["row_count"], body["sheet_count"]) == (1, 5, 2)
    dataset_id = body["dataset_id"]
    # 원본은 사용자 파일 이름이 아니라 정해진 이름으로 보관된다.
    stored = list((tmp_path / "data" / "knowledge" / "files" / str(dataset_id)).iterdir())
    assert len(stored) == 1 and stored[0].name.startswith("v1_") and stored[0].suffix == ".xlsx"
    assert not list((tmp_path / "data" / "knowledge" / "files" / "_incoming").iterdir())

    found = client.get("/api/knowledge/search", query_string={"q": "aix 7.2"}).get_json()
    assert found["hit_count"] == 1
    hit = found["groups"][0]["hits"][0]
    assert hit["sheet_name"] == "서버목록" and hit["cells"][0] == "aixprd01"
    assert hit["columns"] == ["호스트명", "IP", "OS", "담당"]
    # 두 시트에 다 있는 낱말은 두 곳 다 나온다.
    assert client.get("/api/knowledge/search?q=lnxweb02").get_json()["hit_count"] == 2
    # 설명 · 태그로도 찾는다.
    by_meta = client.get("/api/knowledge/search?q=ITSM").get_json()
    assert [d["dataset_id"] for d in by_meta["datasets"]] == [dataset_id]

    detail = client.get(f"/api/knowledge/datasets/{dataset_id}").get_json()
    assert detail["dataset"]["tags"] == "AIX, 백업"
    assert detail["can_edit"] is True
    sheet_id = detail["sheets"][0]["id"]
    rows = client.get(f"/api/knowledge/sheets/{sheet_id}/rows", query_string={"q": "박민수"}).get_json()
    assert rows["total"] == 1 and rows["rows"][0]["row_no"] == 3

    page = client.get("/knowledge")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert 'id="page-knowledge"' in html and "PAGE_LOADERS['knowledge']" in html
    assert "지식 관리" in html                         # 사이드바 묶음


def test_like_wildcards_are_literal(portal) -> None:
    client = login(portal)
    upload(client, "코드,설명\r\nA_1,밑줄\r\nAB1,없음\r\n100%,퍼센트\r\n".encode("utf-8"), "w.csv")
    assert client.get("/api/knowledge/search?q=A_1").get_json()["hit_count"] == 1
    assert client.get("/api/knowledge/search?q=100%25").get_json()["hit_count"] == 1
    assert client.get("/api/knowledge/search?q=").status_code == 400


def test_new_version_replaces_what_search_sees(portal) -> None:
    client = login(portal)
    first = upload(client, xlsx_bytes(SERVERS), "s.xlsx", title="서버").get_json()
    changed = {"서버목록": [["호스트명", "OS"], ["aixprd01", "AIX 7.3"]]}
    second = upload(client, xlsx_bytes(changed), "s.xlsx", dataset_id=str(first["dataset_id"]), note="10월말").get_json()
    assert second["version_no"] == 2 and second["dataset_id"] == first["dataset_id"]

    assert client.get("/api/knowledge/search?q=lnxweb02").get_json()["hit_count"] == 0
    assert client.get("/api/knowledge/search?q=7.3").get_json()["hit_count"] == 1
    detail = client.get(f"/api/knowledge/datasets/{first['dataset_id']}").get_json()
    assert [v["version_no"] for v in detail["versions"]] == [2, 1]
    # 지난 버전도 열어 볼 수 있다.
    old = client.get(f"/api/knowledge/datasets/{first['dataset_id']}?version={first['version_id']}").get_json()
    assert [s["sheet_name"] for s in old["sheets"]] == ["서버목록", "백업정책"]

    same = upload(client, xlsx_bytes(changed), "s.xlsx", dataset_id=str(first["dataset_id"]))
    assert same.status_code == 409


def test_same_file_elsewhere_is_reported(portal) -> None:
    client = login(portal)
    upload(client, xlsx_bytes(SERVERS), "a.xlsx", title="원본")
    again = upload(client, xlsx_bytes(SERVERS), "b.xlsx", title="사본").get_json()
    assert [d["title"] for d in again["duplicates"]] == ["원본"]


def test_only_the_uploader_or_an_admin_can_change(portal) -> None:
    owner = login(portal, "hong")
    other = login(portal, "kim", name="김")
    admin = login(portal, "root", role="admin", name="관리자")
    dataset_id = upload(owner, xlsx_bytes(SERVERS), "s.xlsx", title="서버").get_json()["dataset_id"]

    assert other.get(f"/api/knowledge/datasets/{dataset_id}").get_json()["can_edit"] is False
    assert other.patch(f"/api/knowledge/datasets/{dataset_id}", json={"title": "x"}).status_code == 403
    assert other.delete(f"/api/knowledge/datasets/{dataset_id}").status_code == 403
    assert upload(other, xlsx_bytes({"a": [["x", "y"], [1, 2]]}), "n.xlsx",
                  dataset_id=str(dataset_id)).status_code == 403

    edited = owner.patch(f"/api/knowledge/datasets/{dataset_id}", json={"category": "인프라"}).get_json()
    assert edited["dataset"]["category"] == "인프라" and edited["dataset"]["title"] == "서버"
    assert owner.patch(f"/api/knowledge/datasets/{dataset_id}", json={"title": " "}).status_code == 400

    assert admin.delete(f"/api/knowledge/datasets/{dataset_id}").status_code == 200
    assert owner.get(f"/api/knowledge/datasets/{dataset_id}").status_code == 404
    assert owner.get("/api/knowledge/search?q=aixprd01").get_json()["hit_count"] == 0
    assert owner.get("/api/knowledge/datasets").get_json()["datasets"] == []

    actions = [e["action"] for e in admin.get("/api/knowledge/events").get_json()["events"]]
    assert actions[:3] == ["DELETE", "EDIT", "UPLOAD"]
    assert owner.get("/api/knowledge/events").status_code == 403


def test_download_returns_the_original_and_is_logged(portal) -> None:
    client = login(portal)
    data = xlsx_bytes(SERVERS)
    body = upload(client, data, "서버 현황.xlsx").get_json()
    res = client.get(f"/api/knowledge/versions/{body['version_id']}/download")
    assert res.status_code == 200 and res.data == data
    assert "attachment" in res.headers["Content-Disposition"]
    detail = client.get(f"/api/knowledge/datasets/{body['dataset_id']}").get_json()
    assert detail["events"][0]["action"] == "DOWNLOAD"


def test_bad_files_are_refused_without_leaving_anything(portal, tmp_path) -> None:
    client = login(portal)
    res = upload(client, b"not excel", "x.xlsx")
    assert res.status_code == 400 and "엑셀을 열 수 없습니다" in res.get_json()["error"]
    assert upload(client, b"", "e.csv").status_code == 400
    assert upload(client, b"x", "a.xls").status_code == 400
    assert client.get("/api/knowledge/datasets").get_json()["datasets"] == []
    files = tmp_path / "data" / "knowledge" / "files"
    assert [p for p in files.rglob("*") if p.is_file()] == []


def test_attachments_are_kept_and_found_by_description(portal) -> None:
    client = login(portal)
    body = upload(client, b"%PDF-1.4 ...", "DR훈련.pdf", description="2026 상반기 DR 전환 훈련 결과").get_json()
    assert body["searchable"] is False and body["row_count"] == 0
    found = client.get("/api/knowledge/search?q=DR 훈련").get_json()
    assert [d["dataset_id"] for d in found["datasets"]] == [body["dataset_id"]]
    assert found["groups"] == []


def test_access_follows_the_menu_permission(portal, tmp_path) -> None:
    anonymous = portal.test_client()
    assert anonymous.get("/api/knowledge/summary").status_code == 401

    # access: explicit 이면 부여받지 않은 사용자는 API 도 막힌다.
    registry = portal.extensions["module_registry"]
    path = tmp_path / "config" / "modules" / "knowledge.yaml"
    path.write_text(path.read_text(encoding="utf-8").replace("access: role", "access: explicit"), encoding="utf-8")
    from asset_sync.config import load_config

    registry.reload_from_config(load_config().modules)
    assert login(portal).get("/api/knowledge/summary").status_code == 403
    assert login(portal, "root", role="admin").get("/api/knowledge/summary").status_code == 200


def test_dashboard_panel(portal) -> None:
    client = login(portal)
    upload(client, xlsx_bytes(SERVERS), "s.xlsx", title="서버 현황")
    body = client.get("/api/modules/dashboard?modules=knowledge").get_json()
    panel = body["panels"][0]
    assert panel["status"] == "SUCCESS", panel
    assert panel["panel"]["metrics"][0]["value"] == 1
    assert panel["panel"]["table"]["rows"][0][0] == "서버 현황"


# ── 화면과 스크립트의 짝 ──────────────────────────────────────────────────

PAGE = BASE_DIR / "templates" / "modules" / "knowledge" / "page.html"
SCRIPTS = BASE_DIR / "templates" / "modules" / "knowledge" / "scripts.html"


def test_screen_and_script_agree() -> None:
    page, scripts = PAGE.read_text(encoding="utf-8"), SCRIPTS.read_text(encoding="utf-8")
    # 고치기 칸처럼 스크립트가 그려 넣는 칸도 있으므로 양쪽에서 모은다.
    ids = set(re.findall(r'\bid="([^"{}$]+)"', page + scripts))
    for name in re.findall(r"""getElementById\(\s*['"]([^'"{}$]+)['"]""", scripts):
        assert name in ids, f"화면에 없는 칸: {name}"
    functions = set(re.findall(r"\bfunction\s+([A-Za-z_$][\w$]*)\s*\(", scripts))
    for code in re.findall(r'\bon(?:click|change|input|keyup)\s*=\s*"([^"]*)"', page + scripts):
        for name in re.findall(r"(?<![.\w$'])(knowledge[A-Za-z]*)\s*\(", code):
            assert name in functions, f"없는 함수를 부르는 버튼: {name}"
    # 다른 대메뉴와 겹치지 않도록 전역 함수는 모두 접두어를 쓴다.
    assert all(name.startswith("knowledge") for name in functions), functions
