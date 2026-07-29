from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import streamlit as st  # type: ignore

from asset_sync.config import load_config
from asset_sync.db.sqlite_manager import SQLiteManager
from asset_sync.repositories import AssetRepository
from asset_sync.services.dashboard_service import DashboardService


st.set_page_config(page_title="ITSM·vCenter 자산 현황", layout="wide")
st.title("ITSM·vCenter 자산 현황")
st.caption("선택 설치 기능입니다. 핵심 운영 화면은 기존 Flask에 통합되어 있습니다.")

cfg = load_config()
manager = SQLiteManager(cfg.database_path)
manager.initialize()
with manager.connect() as connection:
    repo = AssetRepository(connection)
    summary = DashboardService(repo).summary()
    changes = repo.changes(limit=500)
    reconciliation = repo.reconciliation(limit=500)

itsm = summary.get("latest_itsm") or {}
rv = summary.get("latest_rvtools") or {}
counts = summary.get("reconciliation_counts") or {}
col1, col2, col3, col4 = st.columns(4)
col1.metric("ITSM 최신 자산", itsm.get("record_count", 0))
col2.metric("vCenter 최신 VM", rv.get("record_count", 0))
col3.metric("MATCHED", counts.get("MATCHED", 0))
col4.metric("검토 대상", sum(int(v) for k, v in counts.items() if k != "MATCHED"))

st.subheader("최근 정합성")
st.dataframe(reconciliation, use_container_width=True, hide_index=True)
st.subheader("최근 변경")
st.dataframe(changes, use_container_width=True, hide_index=True)
