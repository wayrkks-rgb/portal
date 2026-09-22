"""HMC 에서 AIX 프레임과 LPAR 을 읽는다.

vCenter 는 PowerCLI 라는 별도 프로그램을 거쳐야 하지만 HMC 는 REST API 하나로
끝난다. 그래서 통합기 수집과 달리 PowerShell 도, 임시 파일도, 별도 설치도 없다.

받아오는 것은 두 가지다.

* **ManagedSystem** -- 물리 프레임. 장착된 CPU·메모리와 남은 양을 알려준다.
  통합기(ESXi) 에 해당한다.
* **LogicalPartition** -- LPAR. 프레임을 나눠 쓰는 논리 서버로, VM 에 해당한다.

HMC 의 XML 은 펌웨어 버전마다 네임스페이스와 요소 위치가 조금씩 다르다. 그래서
경로로 찾지 않고 태그 이름으로 찾는다. 버전이 올라가도 이름은 잘 바뀌지 않는다.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from ..config import AppConfig
from .hmc_client import DEFAULT_PORT, HMCClient, HMCError, find_all, find_text, local_name

LOGGER = logging.getLogger(__name__)

#: 프레임 한 대에서 뽑을 값. 키가 우리 이름, 값이 HMC XML 의 태그 이름이다.
FRAME_FIELDS: dict[str, str] = {
    "system_name": "SystemName",
    "machine_type": "MachineType",
    "model": "Model",
    "serial_number": "SerialNumber",
    "state": "State",
    "firmware": "SystemFirmware",
    "installed_proc_units": "InstalledSystemProcessorUnits",
    "available_proc_units": "CurrentAvailableSystemProcessorUnits",
    "installed_memory_mb": "InstalledSystemMemory",
    "available_memory_mb": "CurrentAvailableSystemMemory",
}

#: LPAR 한 대에서 뽑을 값.
LPAR_FIELDS: dict[str, str] = {
    "partition_name": "PartitionName",
    "partition_id": "PartitionID",
    "partition_state": "PartitionState",
    "partition_type": "PartitionType",
    "os_version": "OperatingSystemVersion",
    "rmc_ip": "ResourceMonitoringIPAddress",
    "memory_mb": "CurrentMemory",
    "proc_mode": "CurrentProcessingMode",
    "proc_units": "CurrentProcessingUnits",
    "virtual_procs": "CurrentMaximumVirtualProcessors",
    "dedicated_procs": "CurrentDedicatedProcessors",
}

_NUMERIC = {
    "installed_proc_units", "available_proc_units", "installed_memory_mb",
    "available_memory_mb", "memory_mb", "proc_units", "virtual_procs", "dedicated_procs",
    "partition_id",
}


def normalize_endpoint(item: dict[str, Any]) -> dict[str, Any]:
    """설정 한 줄을 다듬는다. 화면에서 온 값과 파일에서 읽은 값을 같게 만든다."""
    entry = dict(item or {})
    entry["id"] = str(entry.get("id") or "").strip()
    entry["name"] = str(entry.get("name") or entry["id"] or "").strip()
    entry["host"] = str(entry.get("host") or entry.get("address") or "").strip()
    entry["username"] = str(entry.get("username") or "").strip()
    entry["password"] = str(entry.get("password") or "")
    try:
        entry["port"] = int(entry.get("port") or DEFAULT_PORT)
    except (TypeError, ValueError):
        entry["port"] = DEFAULT_PORT
    entry["verify_tls"] = bool(entry.get("verify_tls", False))
    entry["enabled"] = bool(entry.get("enabled", True))
    return entry


def _number(value: Any) -> Any:
    if value in (None, ""):
        return None
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return value
    return int(number) if number.is_integer() else round(number, 2)


def _read(element: Any, fields: dict[str, str]) -> dict[str, Any]:
    row = {name: find_text(element, tag) for name, tag in fields.items()}
    for name in _NUMERIC:
        if name in row:
            row[name] = _number(row[name])
    return row


def _entry_uuid(entry: Any) -> str | None:
    """Atom 항목의 id. ``.../ManagedSystem/<uuid>`` 꼴이라 마지막 칸이 uuid 다."""
    for node in entry.iter():
        if local_name(node.tag) == "id" and (node.text or "").strip():
            return node.text.strip().rsplit("/", 1)[-1]
    return None


class HMCCollector:
    """설정에 등록된 HMC 를 돌면서 프레임과 LPAR 을 읽는다."""

    def __init__(self, config: AppConfig, client_factory: Callable[..., HMCClient] | None = None) -> None:
        self.config = config
        self.settings = getattr(config, "hmc", {}) or {}
        self._client_factory = client_factory or HMCClient

    # ── 설정 ────────────────────────────────────────────────────────────
    def enabled(self) -> bool:
        return bool(self.settings.get("enabled", False))

    def endpoints(self, only_enabled: bool = True) -> list[dict[str, Any]]:
        items = [normalize_endpoint(item) for item in self.settings.get("endpoints", []) or []]
        return [item for item in items if item["enabled"]] if only_enabled else items

    def _client(self, entry: dict[str, Any]) -> HMCClient:
        return self._client_factory(
            entry["host"], entry["username"], entry["password"],
            port=entry["port"], verify_tls=entry["verify_tls"],
            timeout_seconds=int(self.settings.get("timeout_seconds", 30) or 30),
        )

    # ── 수집 ────────────────────────────────────────────────────────────
    def collect_one(self, entry: dict[str, Any]) -> dict[str, Any]:
        """HMC 한 대. 프레임 목록을 받고, 프레임마다 LPAR 목록을 받는다."""
        item = normalize_endpoint(entry)
        client = self._client(item)
        frames: list[dict[str, Any]] = []
        partitions: list[dict[str, Any]] = []
        try:
            client.logon()
            feed = client.get("/rest/api/uom/ManagedSystem")
            for atom_entry in find_all(feed, "entry"):
                uuid = _entry_uuid(atom_entry)
                frame = _read(atom_entry, FRAME_FIELDS)
                frame.update({
                    "entity_type": "FRAME", "hmc_id": item["id"], "hmc_host": item["host"],
                    "system_uuid": uuid,
                })
                frames.append(frame)
                if not uuid:
                    continue
                partitions.extend(self._partitions(client, item, frame, uuid))
        finally:
            client.logoff()
        return {
            "status": "SUCCESS", "hmc_id": item["id"], "hmc_host": item["host"],
            "frames": frames, "partitions": partitions,
            "frame_count": len(frames), "partition_count": len(partitions),
        }

    def _partitions(
        self, client: HMCClient, item: dict[str, Any], frame: dict[str, Any], uuid: str
    ) -> list[dict[str, Any]]:
        feed = client.get(f"/rest/api/uom/ManagedSystem/{uuid}/LogicalPartition")
        rows = []
        for atom_entry in find_all(feed, "entry"):
            row = _read(atom_entry, LPAR_FIELDS)
            row.update({
                "entity_type": "LPAR", "hmc_id": item["id"], "hmc_host": item["host"],
                "system_uuid": uuid, "system_name": frame.get("system_name"),
                "partition_uuid": _entry_uuid(atom_entry),
            })
            rows.append(row)
        return rows

    def collect_all(self) -> dict[str, Any]:
        """등록된 HMC 를 모두 돈다. 한 대가 실패해도 나머지는 계속한다."""
        frames: list[dict[str, Any]] = []
        partitions: list[dict[str, Any]] = []
        failed: dict[str, str] = {}
        succeeded: list[str] = []
        for entry in self.endpoints():
            try:
                result = self.collect_one(entry)
            except Exception as exc:
                failed[entry["id"] or entry["host"]] = str(exc)
                LOGGER.warning("HMC 수집 실패: %s", entry["id"] or entry["host"], exc_info=True)
                continue
            frames.extend(result["frames"])
            partitions.extend(result["partitions"])
            succeeded.append(entry["id"] or entry["host"])
        status = "SUCCESS" if succeeded and not failed else ("PARTIAL" if succeeded else "FAILED")
        return {
            "status": status, "frames": frames, "partitions": partitions,
            "frame_count": len(frames), "partition_count": len(partitions),
            "success_scopes": succeeded, "failed_scopes": failed,
        }

    # ── 연결 테스트 ─────────────────────────────────────────────────────
    def test_one(self, entry: dict[str, Any]) -> dict[str, Any]:
        """화면의 [연결 테스트]. 어디서 막혔는지 단계로 알려준다."""
        item = normalize_endpoint(entry)
        if not item["host"]:
            return {"status": "FAILED", "stage": "CONFIG", "error": "HMC 주소를 입력하세요."}
        client = self._client(item)
        try:
            client.logon()
        except HMCError as exc:
            return {"status": "FAILED", "stage": exc.stage, "error": str(exc),
                    "hmc_id": item["id"], "endpoint": f"{item['host']}:{item['port']}"}
        except Exception as exc:
            return {"status": "FAILED", "stage": "LOGON", "error": str(exc),
                    "hmc_id": item["id"], "endpoint": f"{item['host']}:{item['port']}"}
        try:
            feed = client.get("/rest/api/uom/ManagedSystem")
            entries = find_all(feed, "entry")
            names = [find_text(node, "SystemName") for node in entries]
            return {
                "status": "SUCCESS", "stage": "QUERY", "hmc_id": item["id"],
                "endpoint": f"{item['host']}:{item['port']}",
                "frame_count": len(entries),
                "frames": [name for name in names if name][:20],
            }
        except HMCError as exc:
            return {"status": "FAILED", "stage": exc.stage, "error": str(exc),
                    "hmc_id": item["id"], "endpoint": f"{item['host']}:{item['port']}"}
        except Exception as exc:
            return {"status": "FAILED", "stage": "QUERY", "error": str(exc),
                    "hmc_id": item["id"], "endpoint": f"{item['host']}:{item['port']}"}
        finally:
            client.logoff()

    def test_all(self) -> dict[str, Any]:
        results = [self.test_one(entry) for entry in self.endpoints()]
        if not results:
            return {"status": "FAILED", "error": "테스트할 활성 HMC 가 없습니다.", "results": []}
        failed = [r for r in results if r.get("status") != "SUCCESS"]
        status = "SUCCESS" if not failed else ("PARTIAL" if len(failed) < len(results) else "FAILED")
        return {
            "status": status, "results": results,
            "success_count": len(results) - len(failed), "failed_count": len(failed),
        }
