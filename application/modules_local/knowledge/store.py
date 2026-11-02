"""지식 저장소의 DB · 파일 보관. Flask 에 기대지 않는다(떼어서 WAS 로 옮길 수 있게).

한 자료(dataset)에 버전(version)이 쌓이고, 버전마다 시트(sheet) → 행(row)이 붙는다.
화면 · 검색은 늘 **현재 버전**만 본다. 지난 버전은 열람 · 내려받기만 된다.

검색은 지금은 키워드(LIKE)다. 폐쇄망 LLM 을 붙이는 단계에서는 ``knowledge_row.search_text``
를 그대로 임베딩해 의미 검색으로 바꾸고, 이 파일의 ``search`` 결과를 근거로 답을 만든다.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from .parser import ParsedFile, file_type_of

#: 한 번에 넣는 행 수. SQLite · MySQL 모두 이 정도가 빠르고 패킷 상한에도 안 걸린다.
INSERT_BATCH = 1_000
#: 검색어는 띄어쓰기로 나눠 모두 들어간 행을 찾는다(AND). 너무 많으면 앞쪽만 쓴다.
MAX_TERMS = 6
SEARCH_LIMIT = 200
PAGE_LIMIT = 200

META_FIELDS = ("title", "category", "description", "source_note", "tags")
META_LIMITS = {"title": 300, "category": 100, "description": 4000, "source_note": 500, "tags": 500}


class KnowledgeError(ValueError):
    """화면에 그대로 보여 줄 수 있는 거절."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _actor(user: Mapping[str, Any]) -> tuple[str, str]:
    username = str(user.get("username") or user.get("id") or "")
    return username, str(user.get("name") or username)


def is_admin(user: Mapping[str, Any]) -> bool:
    return str(user.get("role") or "").lower() == "admin"


def can_edit(dataset: Mapping[str, Any], user: Mapping[str, Any]) -> bool:
    """올린 사람과 관리자만 고치고 지운다. 보는 것은 메뉴 권한이 있으면 누구나."""
    return is_admin(user) or str(dataset.get("created_by") or "") == _actor(user)[0]


def clean_meta(payload: Mapping[str, Any], *, partial: bool = False) -> dict[str, str]:
    meta: dict[str, str] = {}
    for name in META_FIELDS:
        if partial and name not in payload:
            continue
        value = " ".join(str(payload.get(name) or "").split()) if name != "description" \
            else str(payload.get(name) or "").strip()
        if name == "tags":
            tags = [tag.strip().lstrip("#") for tag in value.replace(";", ",").split(",")]
            value = ", ".join(dict.fromkeys(tag for tag in tags if tag))
        meta[name] = value[: META_LIMITS[name]]
    if ("title" in meta or not partial) and not meta.get("title"):
        raise KnowledgeError("제목을 적어 주세요.")
    return meta


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def search_terms(query: str) -> list[str]:
    return [term for term in str(query or "").split() if term][:MAX_TERMS]


def _like(term: str) -> str:
    # ESCAPE '!' 는 SQLite · MySQL 이 똑같이 해석한다(백슬래시는 MySQL 만 기본값이다).
    escaped = term.replace("!", "!!").replace("%", "!%").replace("_", "!_")
    return f"%{escaped}%"


def _like_all(column: str, terms: list[str]) -> tuple[str, list[str]]:
    clause = " AND ".join(f"{column} LIKE ? ESCAPE '!'" for _ in terms)
    return clause, [_like(term) for term in terms]


def _dict(row: Any) -> dict[str, Any]:
    return dict(row) if row is not None else {}


class KnowledgeStore:
    def __init__(self, conn: Any, files_dir: Path) -> None:
        self.conn = conn
        self.files_dir = Path(files_dir)

    # ── 기록 ───────────────────────────────────────────────────────────────
    def _event(self, action: str, user: Mapping[str, Any], dataset_id: int | None = None,
               version_id: int | None = None, detail: str = "") -> None:
        actor, actor_name = _actor(user)
        self.conn.execute(
            "INSERT INTO knowledge_event(dataset_id,version_id,action,actor,actor_name,detail,created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (dataset_id, version_id, action, actor, actor_name, detail[:2000], _now()),
        )

    # ── 올리기 ─────────────────────────────────────────────────────────────
    def duplicates(self, sha: str, exclude_dataset: int | None = None) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT d.id, d.title, v.version_no FROM knowledge_version v"
            " JOIN knowledge_dataset d ON d.id = v.dataset_id"
            " WHERE v.file_sha256 = ? AND d.deleted_at IS NULL ORDER BY d.id",
            (sha,),
        ).fetchall()
        return [_dict(row) for row in rows if exclude_dataset is None or int(row["id"]) != exclude_dataset]

    def save_upload(
        self,
        upload: Path,
        filename: str,
        parsed: ParsedFile,
        meta: Mapping[str, Any],
        user: Mapping[str, Any],
        *,
        dataset_id: int | None = None,
        note: str = "",
    ) -> dict[str, Any]:
        """새 자료를 만들거나(``dataset_id`` 없음) 기존 자료에 새 버전을 붙인다."""
        actor, actor_name = _actor(user)
        now = _now()
        sha = sha256_of(upload)
        size = Path(upload).stat().st_size

        if dataset_id is None:
            info = clean_meta(meta)
            cur = self.conn.execute(
                "INSERT INTO knowledge_dataset(title,category,description,source_note,tags,kind,"
                "created_by,created_by_name,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (info["title"], info["category"], info["description"], info["source_note"],
                 info["tags"], parsed.kind, actor, actor_name, now, now),
            )
            dataset_id = int(cur.lastrowid)
            version_no = 1
        else:
            dataset = self.dataset(dataset_id)
            if not can_edit(dataset, user):
                raise KnowledgeError("올린 사람이나 관리자만 새 버전을 올릴 수 있습니다.", 403)
            current = self.version(int(dataset["current_version_id"] or 0)) if dataset.get("current_version_id") else {}
            if current and current.get("file_sha256") == sha:
                raise KnowledgeError("현재 버전과 똑같은 파일입니다. 바뀐 파일을 올려 주세요.", 409)
            row = self.conn.execute(
                "SELECT MAX(version_no) AS no FROM knowledge_version WHERE dataset_id = ?", (dataset_id,)
            ).fetchone()
            version_no = int((row["no"] if row else 0) or 0) + 1
            self.conn.execute(
                "UPDATE knowledge_dataset SET kind = ?, updated_at = ? WHERE id = ?",
                (parsed.kind, now, dataset_id),
            )

        duplicates = self.duplicates(sha, exclude_dataset=dataset_id)
        file_type = parsed.file_type or file_type_of(filename)
        stored_name = f"{dataset_id}/v{version_no}_{sha[:12]}.{file_type}"
        cur = self.conn.execute(
            "INSERT INTO knowledge_version(dataset_id,version_no,file_name,stored_name,file_size,"
            "file_sha256,file_type,sheet_count,row_count,searchable,note,uploaded_by,uploaded_by_name,"
            "uploaded_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (dataset_id, version_no, str(filename)[:300], stored_name, size, sha, file_type,
             len(parsed.sheets), parsed.row_count, 1 if parsed.searchable else 0,
             str(note or "").strip()[:1000], actor, actor_name, now),
        )
        version_id = int(cur.lastrowid)

        for index, sheet in enumerate(parsed.sheets):
            cur = self.conn.execute(
                "INSERT INTO knowledge_sheet(version_id,sheet_index,sheet_name,header_row,columns_json,"
                "row_count,truncated) VALUES(?,?,?,?,?,?,?)",
                (version_id, index, sheet.name[:200], sheet.header_row,
                 json.dumps(sheet.columns, ensure_ascii=False), len(sheet.rows), 1 if sheet.truncated else 0),
            )
            sheet_id = int(cur.lastrowid)
            batch: list[tuple[Any, ...]] = []
            for number, values in enumerate(sheet.rows, start=1):
                batch.append((version_id, sheet_id, number, json.dumps(values, ensure_ascii=False),
                              " | ".join(value for value in values if value)))
                if len(batch) >= INSERT_BATCH:
                    self._insert_rows(batch)
                    batch = []
            self._insert_rows(batch)

        self.conn.execute(
            "UPDATE knowledge_dataset SET current_version_id = ?, updated_at = ? WHERE id = ?",
            (version_id, now, dataset_id),
        )
        self._event("UPLOAD" if version_no == 1 else "NEW_VERSION", user, dataset_id, version_id,
                    f"{filename} · {parsed.row_count:,}행 · {len(parsed.sheets)}시트")

        # 파일은 마지막에 옮긴다. 여기서 실패하면 예외가 나가 DB 도 되돌려진다.
        target = self.files_dir / stored_name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(upload, target)

        return {
            "dataset_id": dataset_id,
            "version_id": version_id,
            "version_no": version_no,
            "row_count": parsed.row_count,
            "sheet_count": len(parsed.sheets),
            "kind": parsed.kind,
            "searchable": parsed.searchable,
            "warnings": list(parsed.warnings),
            "duplicates": duplicates,
        }

    def _insert_rows(self, batch: list[tuple[Any, ...]]) -> None:
        if batch:
            self.conn.executemany(
                "INSERT INTO knowledge_row(version_id,sheet_id,row_no,cells_json,search_text)"
                " VALUES(?,?,?,?,?)",
                batch,
            )

    # ── 읽기 ───────────────────────────────────────────────────────────────
    _DATASET_SELECT = (
        "SELECT d.*, v.version_no, v.file_name, v.file_type, v.file_size, v.row_count, v.sheet_count,"
        " v.searchable, v.uploaded_at, v.uploaded_by_name"
        " FROM knowledge_dataset d LEFT JOIN knowledge_version v ON v.id = d.current_version_id"
    )

    def dataset(self, dataset_id: int, *, include_deleted: bool = False) -> dict[str, Any]:
        row = self.conn.execute(self._DATASET_SELECT + " WHERE d.id = ?", (int(dataset_id),)).fetchone()
        if row is None or (row["deleted_at"] and not include_deleted):
            raise KnowledgeError("자료를 찾을 수 없습니다.", 404)
        return _dict(row)

    def version(self, version_id: int) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM knowledge_version WHERE id = ?", (int(version_id),)).fetchone()
        if row is None:
            raise KnowledgeError("버전을 찾을 수 없습니다.", 404)
        return _dict(row)

    def datasets(self, *, category: str = "", query: str = "") -> list[dict[str, Any]]:
        sql = self._DATASET_SELECT + " WHERE d.deleted_at IS NULL"
        params: list[Any] = []
        if category:
            sql += " AND d.category = ?"
            params.append(category)
        terms = search_terms(query)
        if terms:
            meta = "(d.title || ' ' || d.category || ' ' || d.description || ' ' || d.source_note" \
                   " || ' ' || d.tags || ' ' || COALESCE(v.file_name, ''))"
            if self.conn.dialect.name == "mysql":
                meta = "CONCAT_WS(' ', d.title, d.category, d.description, d.source_note, d.tags," \
                       " COALESCE(v.file_name, ''))"
            clause, values = _like_all(meta, terms)
            sql += f" AND {clause}"
            params.extend(values)
        sql += " ORDER BY d.updated_at DESC, d.id DESC"
        return [_dict(row) for row in self.conn.execute(sql, params).fetchall()]

    def categories(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT category, COUNT(*) AS count FROM knowledge_dataset WHERE deleted_at IS NULL"
            " GROUP BY category ORDER BY category"
        ).fetchall()
        return [{"category": row["category"], "count": int(row["count"])} for row in rows]

    def summary(self) -> dict[str, Any]:
        totals = _dict(self.conn.execute(
            "SELECT COUNT(*) AS datasets, COALESCE(SUM(v.row_count), 0) AS rows_total,"
            " COALESCE(SUM(v.file_size), 0) AS bytes_total"
            " FROM knowledge_dataset d LEFT JOIN knowledge_version v ON v.id = d.current_version_id"
            " WHERE d.deleted_at IS NULL"
        ).fetchone())
        month = datetime.now().strftime("%Y-%m")
        uploads = self.conn.execute(
            "SELECT COUNT(*) AS n FROM knowledge_version v JOIN knowledge_dataset d ON d.id = v.dataset_id"
            " WHERE d.deleted_at IS NULL AND v.uploaded_at >= ?",
            (f"{month}-01",),
        ).fetchone()
        return {
            "datasets": int(totals.get("datasets") or 0),
            "rows": int(totals.get("rows_total") or 0),
            "bytes": int(totals.get("bytes_total") or 0),
            "uploads_this_month": int(uploads["n"] if uploads else 0),
            "categories": self.categories(),
            "recent": self.datasets()[:5],
        }

    def detail(self, dataset_id: int, user: Mapping[str, Any]) -> dict[str, Any]:
        dataset = self.dataset(dataset_id)
        versions = [_dict(row) for row in self.conn.execute(
            "SELECT id, version_no, file_name, file_type, file_size, row_count, sheet_count, searchable,"
            " note, uploaded_by_name, uploaded_at FROM knowledge_version WHERE dataset_id = ?"
            " ORDER BY version_no DESC",
            (int(dataset_id),),
        ).fetchall()]
        events = [_dict(row) for row in self.conn.execute(
            "SELECT action, actor_name, detail, created_at FROM knowledge_event WHERE dataset_id = ?"
            " ORDER BY id DESC LIMIT 30",
            (int(dataset_id),),
        ).fetchall()]
        return {"dataset": dataset, "versions": versions, "events": events, "can_edit": can_edit(dataset, user)}

    def sheets(self, version_id: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT id, sheet_index, sheet_name, header_row, columns_json, row_count, truncated"
            " FROM knowledge_sheet WHERE version_id = ? ORDER BY sheet_index",
            (int(version_id),),
        ).fetchall()
        out = []
        for row in rows:
            item = _dict(row)
            item["columns"] = json.loads(item.pop("columns_json") or "[]")
            out.append(item)
        return out

    def sheet(self, sheet_id: int) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT s.*, v.dataset_id FROM knowledge_sheet s JOIN knowledge_version v ON v.id = s.version_id"
            " WHERE s.id = ?",
            (int(sheet_id),),
        ).fetchone()
        if row is None:
            raise KnowledgeError("시트를 찾을 수 없습니다.", 404)
        item = _dict(row)
        item["columns"] = json.loads(item.pop("columns_json") or "[]")
        self.dataset(int(item["dataset_id"]))  # 지운 자료의 시트는 열지 않는다
        return item

    def rows(self, sheet_id: int, *, query: str = "", offset: int = 0, limit: int = 50) -> dict[str, Any]:
        sheet = self.sheet(sheet_id)
        limit = max(1, min(int(limit or 50), PAGE_LIMIT))
        offset = max(0, int(offset or 0))
        where = "sheet_id = ?"
        params: list[Any] = [int(sheet_id)]
        terms = search_terms(query)
        if terms:
            clause, values = _like_all("search_text", terms)
            where += f" AND {clause}"
            params.extend(values)
        total = self.conn.execute(f"SELECT COUNT(*) AS n FROM knowledge_row WHERE {where}", params).fetchone()
        rows = self.conn.execute(
            f"SELECT row_no, cells_json FROM knowledge_row WHERE {where} ORDER BY row_no LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()
        return {
            "sheet": sheet,
            "total": int(total["n"] if total else 0),
            "offset": offset,
            "limit": limit,
            "terms": terms,
            "rows": [{"row_no": int(row["row_no"]), "cells": json.loads(row["cells_json"])} for row in rows],
        }

    def search(self, query: str, *, category: str = "", limit: int = SEARCH_LIMIT) -> dict[str, Any]:
        """현재 버전의 행과 자료 설명을 함께 찾는다. 결과는 자료별로 묶어 돌려준다."""
        terms = search_terms(query)
        if not terms:
            raise KnowledgeError("검색어를 적어 주세요.")
        limit = max(1, min(int(limit or SEARCH_LIMIT), SEARCH_LIMIT))
        clause, values = _like_all("r.search_text", terms)
        sql = (
            "SELECT r.row_no, r.cells_json, s.id AS sheet_id, s.sheet_name, s.columns_json,"
            " d.id AS dataset_id, d.title, d.category, d.kind"
            " FROM knowledge_row r"
            " JOIN knowledge_dataset d ON d.current_version_id = r.version_id AND d.deleted_at IS NULL"
            " JOIN knowledge_sheet s ON s.id = r.sheet_id"
            f" WHERE {clause}"
        )
        params: list[Any] = list(values)
        if category:
            sql += " AND d.category = ?"
            params.append(category)
        sql += " ORDER BY d.updated_at DESC, d.id DESC, s.sheet_index, r.row_no LIMIT ?"
        params.append(limit + 1)
        found = self.conn.execute(sql, params).fetchall()
        more = len(found) > limit

        groups: dict[int, dict[str, Any]] = {}
        columns_cache: dict[int, list[str]] = {}
        for row in found[:limit]:
            dataset_id = int(row["dataset_id"])
            group = groups.setdefault(dataset_id, {
                "dataset_id": dataset_id, "title": row["title"], "category": row["category"],
                "kind": row["kind"], "hits": [],
            })
            sheet_id = int(row["sheet_id"])
            if sheet_id not in columns_cache:
                columns_cache[sheet_id] = json.loads(row["columns_json"] or "[]")
            group["hits"].append({
                "sheet_id": sheet_id, "sheet_name": row["sheet_name"], "row_no": int(row["row_no"]),
                "columns": columns_cache[sheet_id], "cells": json.loads(row["cells_json"]),
            })

        by_meta = [
            {"dataset_id": item["id"], "title": item["title"], "category": item["category"],
             "kind": item["kind"], "file_type": item.get("file_type"), "description": item["description"]}
            for item in self.datasets(category=category, query=query)
        ]
        return {
            "terms": terms,
            "groups": list(groups.values()),
            "hit_count": min(len(found), limit),
            "more": more,
            "datasets": by_meta,
        }

    # ── 고치기 · 지우기 · 내려받기 ─────────────────────────────────────────
    def update_meta(self, dataset_id: int, payload: Mapping[str, Any], user: Mapping[str, Any]) -> dict[str, Any]:
        dataset = self.dataset(dataset_id)
        if not can_edit(dataset, user):
            raise KnowledgeError("올린 사람이나 관리자만 고칠 수 있습니다.", 403)
        meta = clean_meta(payload, partial=True)
        if not meta:
            return dataset
        sets = ", ".join(f"{name} = ?" for name in meta)
        self.conn.execute(
            f"UPDATE knowledge_dataset SET {sets}, updated_at = ? WHERE id = ?",
            list(meta.values()) + [_now(), int(dataset_id)],
        )
        changed = [name for name in meta if str(dataset.get(name) or "") != meta[name]]
        self._event("EDIT", user, int(dataset_id), detail=", ".join(changed) or "변경 없음")
        return self.dataset(dataset_id)

    def delete(self, dataset_id: int, user: Mapping[str, Any]) -> None:
        dataset = self.dataset(dataset_id)
        if not can_edit(dataset, user):
            raise KnowledgeError("올린 사람이나 관리자만 지울 수 있습니다.", 403)
        actor, _ = _actor(user)
        self.conn.execute(
            "UPDATE knowledge_dataset SET deleted_at = ?, deleted_by = ? WHERE id = ?",
            (_now(), actor, int(dataset_id)),
        )
        self._event("DELETE", user, int(dataset_id), detail=str(dataset.get("title") or ""))

    def download(self, version_id: int, user: Mapping[str, Any]) -> tuple[Path, str]:
        version = self.version(version_id)
        self.dataset(int(version["dataset_id"]))
        path = (self.files_dir / str(version["stored_name"])).resolve()
        if self.files_dir.resolve() not in path.parents or not path.is_file():
            raise KnowledgeError("보관한 파일을 찾을 수 없습니다. 관리자에게 알려 주세요.", 404)
        self._event("DOWNLOAD", user, int(version["dataset_id"]), int(version_id), str(version["file_name"]))
        return path, str(version["file_name"])

    def events(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT e.action, e.actor_name, e.detail, e.created_at, e.dataset_id, d.title"
            " FROM knowledge_event e LEFT JOIN knowledge_dataset d ON d.id = e.dataset_id"
            " ORDER BY e.id DESC LIMIT ?",
            (max(1, min(int(limit), 500)),),
        ).fetchall()
        return [_dict(row) for row in rows]
