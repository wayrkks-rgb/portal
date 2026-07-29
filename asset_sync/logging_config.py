from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import AppConfig

_IPV4 = re.compile(r"\b((?:\d{1,3}\.){3})(\d{1,3})\b")
_HOST = re.compile(r"\b([A-Za-z][A-Za-z0-9_-]*(?:\.[A-Za-z0-9_-]+)+|[A-Za-z][A-Za-z0-9_-]*\d[A-Za-z0-9_-]*)\b")
_USER = re.compile(r"\b(user|operator|admin)([A-Za-z0-9_-]*)\b", re.IGNORECASE)


class SensitiveMaskFilter(logging.Filter):
    def __init__(self, config: AppConfig) -> None:
        super().__init__()
        self.mask_ip = bool(config.security.get("mask_ip_in_logs", True))
        self.mask_hostname = bool(config.security.get("mask_hostname_in_logs", True))
        self.mask_user = bool(config.security.get("mask_user_id_in_logs", True))

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if self.mask_ip:
            message = _IPV4.sub(lambda m: f"{m.group(1)}xxx", message)
        if self.mask_user:
            message = _USER.sub(lambda m: f"{m.group(1)[:3]}***", message)
        if self.mask_hostname:
            message = _HOST.sub(self._mask_hostname, message)
        record.msg = message
        record.args = ()
        return True

    @staticmethod
    def _mask_hostname(match: re.Match[str]) -> str:
        value = match.group(1)
        if value.isdigit() or value.upper() in {
            "SUCCESS", "FAILED", "PARTIAL_SUCCESS", "WARNING", "CRITICAL", "RVTOOLS", "ORACLE", "ITSM"
        }:
            return value
        if len(value) <= 6:
            return value[:2] + "***"
        return value[:3] + "***" + value[-3:]


def configure_logging(log_dir: Path, level: str = "INFO", config: AppConfig | None = None) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    if root.handlers:
        return
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    mask_filter = SensitiveMaskFilter(config) if config else None
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    file_handler = RotatingFileHandler(log_dir / "asset_sync.log", maxBytes=10_000_000, backupCount=10, encoding="utf-8")
    file_handler.setFormatter(formatter)
    if mask_filter:
        console.addFilter(mask_filter)
        file_handler.addFilter(mask_filter)
    root.addHandler(console)
    root.addHandler(file_handler)
