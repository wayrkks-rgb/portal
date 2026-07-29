from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from openpyxl import Workbook

from ..repositories import AssetRepository


class ExportService:
    def __init__(self, repository: AssetRepository, export_dir: Path) -> None:
        self.repo = repository
        self.export_dir = export_dir

    def export_current(self, filename: str = "asset_sync_result.xlsx") -> Path:
        self.export_dir.mkdir(parents=True, exist_ok=True)
        path = self.export_dir / filename
        workbook = Workbook()
        summary = workbook.active
        summary.title = "01_종합요약"
        itsm = self.repo.latest_snapshot("ITSM")
        rv = self.repo.latest_snapshot("RVTOOLS")
        summary.append(["항목", "값"])
        summary.append(["ITSM 스냅샷", itsm["collected_at"] if itsm else "없음"])
        summary.append(["ITSM 자산수", itsm["record_count"] if itsm else 0])
        summary.append(["vCenter 스냅샷", rv["collected_at"] if rv else "없음"])
        summary.append(["vCenter VM수", rv["record_count"] if rv else 0])

        self._write_rows(workbook, "02_변경이력", self.repo.changes(limit=100000))
        recon = self.repo.reconciliation(limit=100000)
        for row in recon:
            row["drift_json"] = json.loads(row.get("drift_json") or "[]")
        self._write_rows(workbook, "03_정합성", recon)
        self._write_rows(workbook, "04_수집이력", self.repo.collection_runs(10000))
        workbook.save(path)
        return path

    @staticmethod
    def _write_rows(workbook: Workbook, title: str, rows: list[dict[str, Any]]) -> None:
        sheet = workbook.create_sheet(title[:31])
        if not rows:
            sheet.append(["데이터 없음"])
            return
        headers = list(rows[0].keys())
        sheet.append(headers)
        for row in rows:
            sheet.append([json.dumps(row.get(h), ensure_ascii=False) if isinstance(row.get(h), (dict, list)) else row.get(h) for h in headers])
        for column in sheet.columns:
            width = min(max(len(str(cell.value or "")) for cell in column) + 2, 60)
            sheet.column_dimensions[column[0].column_letter].width = max(width, 10)
