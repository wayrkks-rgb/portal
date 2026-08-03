"""HMC 에서 LPAR 정보를 실제로 가져올 수 있는지 확인한다.

IBM Power(E870 · S924 · E980)는 HMC 가 VMware 의 vCenter 역할을 한다. PowerCLI 에
해당하는 공식 도구는 없지만, HMC 의 SSH CLI 로 같은 일을 할 수 있다.

    python scripts\\probe_hmc.py --hmc 10.10.20.30 --user hmcviewer
    python scripts\\probe_hmc.py --hmc 10.10.20.30 --user hmcviewer --json > lpars.json

Windows 10 / Server 2019 이상에 기본 포함된 ssh.exe 를 쓰므로 추가 설치가 없다.
비밀번호를 물어보지 않게 하려면 SSH 키를 HMC 에 등록해 두는 것이 좋다
(HMC 화면: 사용자 관리 → 사용자 → SSH 키 추가).

**이 스크립트는 조회만 한다.** 쓰는 명령은 실행하지 않으므로 조회 전용 계정
(hmcviewer 역할)이면 충분하다.

필드 이름은 HMC 버전마다 조금씩 다르다. 뭔가 비어 있으면 --raw 로 원본을 보고
FIELDS 를 실제 HMC 에 맞춰 고치면 된다.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

# HMC CLI 가 -F 로 받아주는 필드. 버전에 따라 없는 필드가 있으면 그 명령만 실패하므로
# 하나씩 확인하려면 --raw 를 쓴다.
FIELDS = {
    # 통합기(관리 시스템) 목록
    "systems": "lssyscfg -r sys -F name,type_model,serial_num,state",
    # LPAR 기본 정보. rmc_ipaddr 는 RMC 통신용 IP 라 LPAR 의 전체 IP 가 아니다.
    "lpars": (
        "lssyscfg -r lpar -m {system} "
        "-F name,lpar_id,state,os_version,rmc_state,rmc_ipaddr,lpar_env"
    ),
    # 할당된 CPU. 사용률이 아니라 구성값이다.
    "proc": (
        "lshwres -r proc -m {system} --level lpar "
        "-F lpar_name,curr_proc_units,curr_procs,curr_proc_mode,curr_sharing_mode"
    ),
    # 할당된 메모리(MB)
    "mem": "lshwres -r mem -m {system} --level lpar -F lpar_name,curr_mem",
}


def run(hmc: str, user: str, command: str, timeout: int = 30) -> tuple[bool, str]:
    """HMC 에 SSH 로 명령 하나를 보낸다."""
    argv = [
        "ssh",
        "-o", "BatchMode=yes",            # 비밀번호 프롬프트에서 멈추지 않게
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", f"ConnectTimeout={timeout}",
        f"{user}@{hmc}",
        command,
    ]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout + 10)
    except FileNotFoundError:
        return False, "ssh 명령을 찾을 수 없습니다. Windows 기능에서 OpenSSH 클라이언트를 설치하세요."
    except subprocess.TimeoutExpired:
        return False, f"{timeout}초 안에 응답이 없습니다. 방화벽과 HMC 주소를 확인하세요."
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout).strip()
    return True, proc.stdout.strip()


def parse(text: str, columns: list[str]) -> list[dict[str, str]]:
    """-F 출력은 한 줄에 한 건, 쉼표 구분이다."""
    rows: list[dict[str, str]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        # 값 안에 쉼표가 들어가는 필드가 있어 앞에서부터 컬럼 수만큼만 자른다.
        parts = line.split(",")
        if len(parts) < len(columns):
            parts += [""] * (len(columns) - len(parts))
        rows.append(dict(zip(columns, parts)))
    return rows


def columns_of(command: str) -> list[str]:
    return command.split("-F", 1)[1].strip().split(",")


def main() -> int:
    parser = argparse.ArgumentParser(description="HMC LPAR 조회 가능 여부 확인")
    parser.add_argument("--hmc", required=True, help="HMC 주소 또는 FQDN")
    parser.add_argument("--user", default="hscroot", help="HMC 계정 (조회 전용 권장)")
    parser.add_argument("--system", help="이 통합기만 조회 (기본: 전부)")
    parser.add_argument("--json", action="store_true", help="결과를 JSON 으로 출력")
    parser.add_argument("--raw", action="store_true", help="명령 원본 출력을 그대로 보여준다")
    args = parser.parse_args()

    ok, output = run(args.hmc, args.user, FIELDS["systems"])
    if not ok:
        print(f"[실패] HMC 접속: {output}", file=sys.stderr)
        print(
            "\n확인할 것"
            "\n  1. ssh {user}@{hmc} 가 수동으로 되는가"
            "\n  2. 22 번 포트가 열려 있는가"
            "\n  3. 계정에 SSH 키가 등록되어 있는가 (BatchMode 라 비밀번호를 못 받는다)".format(
                user=args.user, hmc=args.hmc
            ),
            file=sys.stderr,
        )
        return 1

    if args.raw:
        print("$ " + FIELDS["systems"])
        print(output)

    systems = parse(output, columns_of(FIELDS["systems"]))
    if args.system:
        systems = [s for s in systems if s["name"] == args.system]
    if not systems:
        print("[실패] 조회된 통합기가 없습니다.", file=sys.stderr)
        return 1

    result: list[dict] = []
    for system in systems:
        name = system["name"]
        entry = {"system": system, "lpars": [], "errors": []}

        tables: dict[str, list[dict[str, str]]] = {}
        for key in ("lpars", "proc", "mem"):
            command = FIELDS[key].format(system=name)
            ok, output = run(args.hmc, args.user, command)
            if args.raw:
                print(f"\n$ {command}")
                print(output if ok else f"(실패) {output}")
            if not ok:
                entry["errors"].append({key: output})
                tables[key] = []
                continue
            tables[key] = parse(output, columns_of(command))

        # LPAR 이름으로 CPU·메모리를 합친다.
        proc_by = {row["lpar_name"]: row for row in tables["proc"]}
        mem_by = {row["lpar_name"]: row for row in tables["mem"]}
        for lpar in tables["lpars"]:
            merged = dict(lpar)
            merged.update({k: v for k, v in proc_by.get(lpar["name"], {}).items() if k != "lpar_name"})
            merged.update({k: v for k, v in mem_by.get(lpar["name"], {}).items() if k != "lpar_name"})
            entry["lpars"].append(merged)
        result.append(entry)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    for entry in result:
        system = entry["system"]
        print(f"\n■ {system['name']}  ({system.get('type_model')} / {system.get('serial_num')})"
              f"  상태={system.get('state')}")
        if entry["errors"]:
            for error in entry["errors"]:
                print(f"   [경고] {error}")
        if not entry["lpars"]:
            print("   LPAR 없음")
            continue
        print(f"   {'LPAR 이름':<24}{'상태':<12}{'CPU(EC)':<10}{'VP':<6}{'MEM(MB)':<10}"
              f"{'RMC':<10}{'RMC IP':<16}OS")
        print("   " + "-" * 108)
        for lpar in entry["lpars"]:
            print(f"   {lpar.get('name',''):<24}{lpar.get('state',''):<12}"
                  f"{lpar.get('curr_proc_units',''):<10}{lpar.get('curr_procs',''):<6}"
                  f"{lpar.get('curr_mem',''):<10}{lpar.get('rmc_state',''):<10}"
                  f"{lpar.get('rmc_ipaddr',''):<16}{lpar.get('os_version','')}")

    print(
        "\n※ CPU·MEM 은 '할당량'이지 사용량이 아닙니다."
        "\n※ RMC IP 는 HMC 통신용 IP 하나뿐입니다. LPAR 의 전체 IP 와 hostname 은"
        "\n   각 AIX 에서 따로 조회해야 정확합니다 (hostname · netstat -in · prtconf)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
