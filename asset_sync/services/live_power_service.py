"""VM 의 '지금' 전원 상태를 통합기에서 바로 읽는다.

일간 보고의 전원 상태는 아침 배치 시점 값이다. 하루에도 꺼졌다 켜지는 VM 은 낮에
보면 다르다. 그래서 **전원이 바뀐 VM 에 한해**, 그 VM 이 있는 통합기 1대에만 붙어
지금 상태를 읽는다.

느리면 쓸모가 없으므로 두 길을 둔다(설정 ``vcenter.live_power_method``).

- ``REST``     vSphere REST API. PowerCLI 모듈을 안 읽으므로 보통 1~3초.
- ``POWERCLI`` 배치와 같은 PowerCLI. 모듈 로딩만 6~15초라 보통 10~25초.
- ``AUTO``     (기본) REST 를 먼저 쓰고, 안 되면 PowerCLI 로 다시 한다.
               Windows 통합 인증(PASS_THROUGH) 통합기는 REST 로 못 붙으므로 바로 PowerCLI.

어느 길로 몇 초 걸렸는지(모듈 · 접속 · 조회)를 결과에 담는다. 오래 걸리면 화면에서
바로 보이고, 설정으로 길을 바꿀 수 있다.
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Iterable

from ..collectors.powercli_collector import PowerCLICollectionError, PowerCLICollector

LOGGER = logging.getLogger(__name__)

POWER_LABELS = {"poweredon": "켜짐", "poweredoff": "꺼짐", "suspended": "일시중지"}
METHODS = ("AUTO", "REST", "POWERCLI")
#: 한 번에 묻는 VM 상한. 화면 버튼 하나가 통합기를 오래 붙잡지 않게 한다.
MAX_VMS = 300


def normalize_power(value: Any) -> str:
    """``POWERED_ON`` · ``poweredOn`` · ``PoweredOn`` → ``poweredon`` (스냅샷과 같은 모양)."""
    return str(value or "").replace("_", "").replace(" ", "").strip().lower()


def power_label(value: Any) -> str:
    key = normalize_power(value)
    return POWER_LABELS.get(key, key or "-")


def _moref_value(vm_id: Any) -> str:
    """스냅샷의 ``virtualmachine-vm-123`` → ``vm-123``."""
    text = str(vm_id or "").strip().lower()
    return text.split("virtualmachine-", 1)[1] if text.startswith("virtualmachine-") else text


class LivePowerError(RuntimeError):
    pass


class LivePowerService:
    def __init__(self, config: Any, collector: PowerCLICollector | None = None) -> None:
        self.config = config
        self.collector = collector or PowerCLICollector(config)

    # ── 설정 ────────────────────────────────────────────────────────────
    @property
    def method(self) -> str:
        value = str(self.config.rvtools.get("live_power_method", "AUTO")).strip().upper()
        return value if value in METHODS else "AUTO"

    def _entry(self, vcenter_id: str) -> dict[str, Any]:
        for entry in self.config.rvtools.get("vcenters", []) or []:
            if str(entry.get("id") or entry.get("name") or "") == vcenter_id:
                return self.collector._effective_entry(entry)
        raise LivePowerError(f"연계 설정에 없는 통합기입니다: {vcenter_id}")

    # ── 바깥에서 부르는 것 ──────────────────────────────────────────────
    def lookup(self, vms: Iterable[dict[str, Any]]) -> dict[str, Any]:
        """``vms``: 스냅샷 레코드(asset_key · vm_id · vm_uuid · vm_name · vcenter · power_state).

        통합기별로 나눠 동시에 묻는다. 한 통합기가 실패해도 나머지는 결과를 낸다.
        """
        groups: dict[str, list[dict[str, Any]]] = {}
        for vm in list(vms)[:MAX_VMS]:
            groups.setdefault(str(vm.get("vcenter") or ""), []).append(vm)
        started = time.monotonic()
        with ThreadPoolExecutor(max_workers=max(1, min(4, len(groups)))) as pool:
            outcomes = list(pool.map(lambda item: self._lookup_one(*item), groups.items()))
        results = [row for outcome in outcomes for row in outcome.pop("rows")]
        return {
            "status": "SUCCESS" if all(o["status"] == "SUCCESS" for o in outcomes) else (
                "PARTIAL" if any(o["status"] == "SUCCESS" for o in outcomes) else "FAILED"),
            "elapsed_seconds": round(time.monotonic() - started, 2),
            "vcenters": outcomes,
            "results": results,
            "method_setting": self.method,
        }

    def _lookup_one(self, vcenter_id: str, vms: list[dict[str, Any]]) -> dict[str, Any]:
        started = time.monotonic()
        outcome: dict[str, Any] = {"vcenter_id": vcenter_id, "count": len(vms), "attempts": []}
        try:
            entry = self._entry(vcenter_id)
        except LivePowerError as exc:
            return self._failed(outcome, vms, str(exc), started)

        order = ["REST", "POWERCLI"] if self.method == "AUTO" else [self.method]
        if str(entry.get("auth_mode") or "").upper() == "PASS_THROUGH" and "REST" in order and len(order) > 1:
            order.remove("REST")  # Windows 통합 인증은 REST 로 못 붙는다.
        live: dict[str, dict[str, Any]] | None = None
        for method in order:
            tried = time.monotonic()
            try:
                found, timings = (self._via_rest if method == "REST" else self._via_powercli)(entry, vms)
                outcome["attempts"].append({"method": method, "status": "SUCCESS", "timings": timings,
                                            "seconds": round(time.monotonic() - tried, 2)})
                outcome["method"] = method
                live = found
                break
            except Exception as exc:  # 다음 길로 넘어간다. 이유는 화면에 남긴다.
                LOGGER.warning("지금 전원 조회 실패 (%s · %s): %s", vcenter_id, method, exc)
                outcome["attempts"].append({"method": method, "status": "FAILED", "error": str(exc)[:500],
                                            "seconds": round(time.monotonic() - tried, 2)})
        if live is None:
            last = outcome["attempts"][-1]["error"] if outcome["attempts"] else "조회하지 못했습니다."
            return self._failed(outcome, vms, last, started)

        outcome["rows"] = [self._compare(vm, live) for vm in vms]
        outcome["status"] = "SUCCESS"
        outcome["seconds"] = round(time.monotonic() - started, 2)
        outcome["vcenter_label"] = entry.get("name") or vcenter_id
        return outcome

    @staticmethod
    def _failed(outcome: dict[str, Any], vms: list[dict[str, Any]], error: str, started: float) -> dict[str, Any]:
        outcome.update({
            "status": "FAILED", "error": error, "seconds": round(time.monotonic() - started, 2),
            "rows": [{"vm_key": vm.get("asset_key"), "found": False, "error": error,
                      "batch_power": normalize_power(vm.get("power_state"))} for vm in vms],
        })
        return outcome

    @staticmethod
    def _keys(vm: dict[str, Any]) -> list[str]:
        return [key for key in (_moref_value(vm.get("vm_id")), str(vm.get("vm_uuid") or "").lower()) if key]

    def _compare(self, vm: dict[str, Any], live: dict[str, dict[str, Any]]) -> dict[str, Any]:
        hit = next((live[key] for key in self._keys(vm) if key in live), None)
        batch = normalize_power(vm.get("power_state"))
        if hit is None:
            return {"vm_key": vm.get("asset_key"), "found": False, "batch_power": batch,
                    "error": "통합기에서 이 VM 을 찾지 못했습니다(삭제 · 이동 가능성)."}
        now = normalize_power(hit.get("power"))
        return {
            "vm_key": vm.get("asset_key"), "found": True, "vm_name": hit.get("name") or vm.get("vm_name"),
            "batch_power": batch, "batch_label": power_label(batch),
            "now_power": now, "now_label": power_label(now), "same": now == batch,
        }

    # ── REST ────────────────────────────────────────────────────────────
    def _via_rest(self, entry: dict[str, Any], vms: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], dict[str, float]]:
        try:
            import requests  # BFF 용으로 이미 반입하는 패키지다.
        except ImportError as exc:
            raise LivePowerError("requests 패키지가 없어 REST 로 조회할 수 없습니다.") from exc
        if str(entry.get("auth_mode") or "").upper() != "CREDENTIAL":
            raise LivePowerError("REST 는 계정 인증(CREDENTIAL)만 됩니다.")
        base = f"https://{entry['server']}:{int(entry.get('port') or 443)}"
        verify = not bool(entry.get("bypass_ssl_check"))
        timeout = float(self.config.rvtools.get("live_power_rest_timeout_seconds", 10))
        auth = (str(entry.get("username") or ""), str(entry.get("password") or ""))
        morefs = [value for value in (_moref_value(vm.get("vm_id")) for vm in vms) if value]
        if not morefs:
            raise LivePowerError("VM ID(MoRef)가 없어 REST 로 찾을 수 없습니다.")
        timings: dict[str, float] = {}
        mark = time.monotonic()
        session = requests.Session()
        session.verify = verify
        # 통합기는 내부망이다. 서버에 프록시 환경 변수가 있어도 거치지 않는다(필요하면 설정으로 켠다).
        session.trust_env = bool(self.config.rvtools.get("live_power_rest_use_proxy", False))
        # vSphere 7.0U2+ 는 /api, 그 전(6.5~7.0U1)은 /rest. 새 쪽을 먼저 본다.
        response = session.post(f"{base}/api/session", auth=auth, timeout=timeout)
        flavor = "api"
        if response.status_code == 404:
            response = session.post(f"{base}/rest/com/vmware/cis/session", auth=auth, timeout=timeout)
            flavor = "rest"
        if response.status_code in (401, 403):
            raise LivePowerError(f"vCenter 로그인 거부(HTTP {response.status_code}).")
        response.raise_for_status()
        token = response.json() if flavor == "api" else response.json().get("value")
        session.headers["vmware-api-session-id"] = str(token)
        timings["connect"] = round(time.monotonic() - mark, 2)
        mark = time.monotonic()
        try:
            found: dict[str, dict[str, Any]] = {}
            for start in range(0, len(morefs), 100):  # 주소 길이 제한을 피한다.
                chunk = morefs[start:start + 100]
                if flavor == "api":
                    reply = session.get(f"{base}/api/vcenter/vm", params=[("vms", m) for m in chunk], timeout=timeout)
                    items = reply.json() if reply.ok else None
                else:
                    reply = session.get(f"{base}/rest/vcenter/vm", params=[("filter.vms", m) for m in chunk], timeout=timeout)
                    items = reply.json().get("value") if reply.ok else None
                if items is None:
                    raise LivePowerError(f"VM 조회 실패(HTTP {reply.status_code}).")
                for item in items:
                    found[str(item.get("vm") or "").lower()] = {"name": item.get("name"), "power": item.get("power_state")}
            timings["query"] = round(time.monotonic() - mark, 2)
            return found, timings
        finally:
            try:
                session.delete(f"{base}/api/session" if flavor == "api" else f"{base}/rest/com/vmware/cis/session",
                               timeout=timeout)
            except Exception:  # 로그아웃 실패는 결과에 영향이 없다(세션은 시간이 지나면 끝난다).
                pass

    # ── PowerCLI ────────────────────────────────────────────────────────
    def _via_powercli(self, entry: dict[str, Any], vms: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], dict[str, float]]:
        executable = self.collector._resolve_executable()
        if not executable:
            raise LivePowerError("PowerShell 실행 파일을 찾을 수 없습니다.")
        script = self.config.resolve(self.config.rvtools.get("live_power_script", "scripts/get_vm_power_state.ps1"))
        if not script.exists():
            raise LivePowerError(f"스크립트가 없습니다: {script}")
        try:
            env = self.collector._build_environment(entry, self.config.resolve("data/temp/powercli/unused.json"))
        except PowerCLICollectionError as exc:
            raise LivePowerError(str(exc)) from exc
        keys = sorted({key for vm in vms for key in self._keys(vm)})
        env["VM_KEYS"] = ",".join(keys)
        timeout = int(self.config.rvtools.get("live_power_timeout_seconds", 90))
        mark = time.monotonic()
        try:
            proc = subprocess.run(
                [executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                 "-File", str(script)],
                shell=False, capture_output=True, timeout=timeout, cwd=str(self.config.root_dir), env=env,
            )
        except subprocess.TimeoutExpired as exc:
            raise LivePowerError(f"PowerCLI 가 {timeout}초 안에 끝나지 않았습니다.") from exc
        total = round(time.monotonic() - mark, 2)
        stdout = (proc.stdout or b"").decode("utf-8", errors="replace")
        line = next((l for l in stdout.splitlines() if l.startswith("RESULT_JSON=")), None)
        if proc.returncode != 0 or line is None:
            stderr = (proc.stderr or b"").decode("utf-8", errors="replace")
            raise LivePowerError((stderr or stdout or "PowerCLI 실행 실패")[-800:])
        data = json.loads(line[len("RESULT_JSON="):])
        rows = data.get("vms") or []
        if isinstance(rows, dict):
            rows = [rows]
        found: dict[str, dict[str, Any]] = {}
        for row in rows:
            for key in (row.get("moref"), row.get("instance_uuid"), row.get("uuid")):
                if key:
                    found[str(key).lower()] = {"name": row.get("name"), "power": row.get("power")}
        timings = {str(k): float(v) for k, v in (data.get("timings") or {}).items()}
        # 프로세스 띄우기 등 스크립트 밖에서 쓴 시간.
        timings["process"] = round(max(0.0, total - sum(timings.values())), 2)
        return found, timings
