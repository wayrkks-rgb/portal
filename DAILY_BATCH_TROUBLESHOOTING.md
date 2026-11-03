# 일일 배치가 안 돌 때 — 원인 찾기와 조치

배치가 안 도는 원인은 거의 항상 아래 다섯 가지 중 하나다. **순서대로 보면 된다.**
대부분은 1단계의 점검 스크립트 하나로 원인과 조치가 함께 나온다.

---

## 1단계 · 점검 스크립트 한 번 (2분)

운영 서버에서 소스 폴더로 이동한 뒤:

```bat
.venv\Scripts\python.exe scripts\check_daily_batch.py --days 7
```

맨 아래 **「진단 요약 — 원인 → 조치」** 만 읽어도 된다. 거기 나온 조치를 하고
2단계(수동 실행)로 확인한다. 요약에 아무것도 없으면 3단계 표를 본다.

> 이 스크립트는 작업 등록·실행 계정·로그온 방식·마지막 결과 코드, 화면의 배치
> 켜짐 여부, DB 배치 기록, 스냅샷, 남은 잠금, 로그의 마지막 실행 한 벌을 본다.

## 2단계 · 손으로 한 번 돌려 확인

```bat
scripts\run_daily_batch.bat
type logs\daily_batch_console.log
```

`.bat` 은 **cmd 가 실행하는 파일**이다. 앞에 `python` 을 붙이거나 PyCharm 의 ▶ Run 으로
돌리면 파이썬이 배치 파일을 파이썬 코드로 읽어 `SyntaxError: leading zeros in decimal integer
literals ...`(2번째 줄의 `07:00`) 가 난다. 배치는 실행조차 안 된 것이다.

| 어디서 | 이렇게 |
| --- | --- |
| 명령 프롬프트(cmd) | `scripts\run_daily_batch.bat` |
| PowerShell | `cmd /c scripts\run_daily_batch.bat` |
| PyCharm | 아래 **Terminal** 탭에서 위 명령 (▶ Run 버튼 아님) |

로그의 마지막 `===== BATCH START ... =====` 부터 `===== BATCH END ... / exit N =====`
까지가 한 번의 실행이다.

| 끝 줄 | 뜻 |
|---|---|
| `exit 0` | 성공. 화면에 반영된다 |
| `exit 2` | 수집이 실패(FAILED). 바로 위 `[FAILED] ...` 줄이 사유 |
| `exit 1` + `[STOP]` | 파이썬 가상환경 문제. `scripts\install_offline.bat` 재실행 |
| `[WARN] Pre-check found problems` | 사전 점검에서 설정 문제를 찾았다. 위 `"errors"` 를 본다. 배치는 계속 돈다 |

**손으로는 되는데 07시에만 안 되면 → 거의 확실히 4단계(실행 계정) 문제다.**

## 3단계 · 증상별 원인 표

| 증상 | 원인 | 조치 |
|---|---|---|
| 로그에 그날 `BATCH START` 줄이 아예 없다 | 작업이 깨어나지 않았다 | 작업 스케줄러에서 작업이 **사용**인지, 트리거가 매일 07:00 인지 확인. 없으면 `scripts\register_daily_task.bat` 을 **관리자 권한**으로 실행 |
| 작업 마지막 결과 `0x41303` | 아직 한 번도 안 돌았다 | 작업 스케줄러에서 우클릭 → **실행**으로 시험 |
| 작업 마지막 결과 `0x800710E0` | 실행 계정 암호 만료·권한 부족 | 작업을 다시 등록(아래 5단계) |
| 작업 마지막 결과 `0x80070002` / `0x8007010B` | 소스 폴더를 옮겨 경로가 깨졌다 | 작업을 다시 등록 |
| 로그온 방식이 "사용자가 로그온할 때만 실행" | 07시에 아무도 로그온 안 하면 안 돈다 | `scripts\register_daily_task.bat` 으로 다시 등록(SYSTEM 으로, 로그온 무관) |
| 로그에 `"status": "DISABLED"` | 화면에서 배치를 꺼 뒀다 | 관리 → 연계 설정 → 일일 실행 및 로그 → **일일 배치 사용** |
| 로그에 `"status": "SKIPPED"` · "이미 실행 중" | 배치 잠금이 남아 있다 | 4시간 뒤 저절로 풀린다. 급하면 아래 「잠금 수동 해제」 |
| 로그에 "PowerCLI 모듈을 찾지 못했습니다" | 실행 계정이 PowerCLI 를 못 본다 | **4단계** |
| ITSM 은 되는데 vCenter 만 실패 | 위와 같음, 또는 vCenter 계정·네트워크 | 4단계 → 관리 → 연계 설정 → vCenter **[연결 시험]** |
| vCenter 는 되는데 ITSM 만 실패 | Oracle 계정·SQL 파일·네트워크 | `.venv\Scripts\python.exe scripts\check_oracle_connection.py` |

## 4단계 · 실행 계정(SYSTEM) 문제 — 가장 흔하다

`scripts\register_daily_task.bat` · 화면의 [작업 등록] 은 작업을 **SYSTEM 계정**으로
등록한다. 로그온 여부와 상관없이 돌고 암호가 만료되지 않아서다.

그런데 PowerCLI 를 관리자 계정으로 `Install-Module` 하거나 `Documents` 아래에
복사하면 **그 사람만** 모듈을 본다. 사람이 직접 돌리면 되고 07시 배치(SYSTEM)만
실패하는 이유가 이것이다.

**확인** — 관리자 PowerShell 에서:

```powershell
Get-Module -ListAvailable VMware.VimAutomation.Core |
  Sort-Object Version -Descending | Select-Object -First 1 Name, Version, Path
```

`Path` 가 `C:\Users\...` 로 시작하면 SYSTEM 은 못 본다.

**조치** — 모듈을 모든 계정이 보는 곳으로 복사:

```powershell
$src = Split-Path (Split-Path (Get-Module -ListAvailable VMware.VimAutomation.Core |
         Sort-Object Version -Descending | Select-Object -First 1).Path)   # ...\Modules
$dst = Join-Path $env:ProgramFiles 'WindowsPowerShell\Modules'
Get-ChildItem $src -Directory -Filter 'VMware.*' | Copy-Item -Destination $dst -Recurse -Force
Get-ChildItem $dst -Recurse | Unblock-File
```

복사 뒤 2단계(수동 실행)를 다시 하고, 작업 스케줄러에서 **실행**을 눌러 로그의
`account NT AUTHORITY\SYSTEM` 실행이 `exit 0` 으로 끝나는지 본다. 사전 점검은
배치 안에서 돌기 때문에, 로그의 사전 점검 결과가 곧 **SYSTEM 기준의 답**이다.

## 5단계 · 작업 다시 등록

둘 중 하나만 쓴다. 둘 다 쓰면 나중 것이 앞의 것을 덮는다.

| 방법 | 실행 계정 | 쓸 때 |
|---|---|---|
| `scripts\register_daily_task.bat` (관리자 권한) | SYSTEM | 기본. PowerCLI 가 Program Files 에 있을 때 |
| `powershell -File scripts\register_daily_task.ps1 -RunAsUser 도메인\계정` | 지정한 계정 | PowerCLI·자격증명을 특정 계정에 둬야 할 때. 그 계정 암호가 바뀌면 다시 등록해야 한다 |

등록 상태만 보기: `scripts\register_daily_task.bat /status`

## 잠금 수동 해제

**돌고 있는 배치가 정말 없을 때만** 한다(작업 관리자에 `python.exe` 가 없을 것).

```bat
.venv\Scripts\python.exe scripts\release_batch_lock.py            & rem 상태만 보기
.venv\Scripts\python.exe scripts\release_batch_lock.py --release  & rem 풀기
```

## 고친 뒤 확인

1. 작업 스케줄러에서 작업 우클릭 → **실행**
2. `check_daily_batch.py` 를 다시 돌려 「진단 요약」 이 비었는지, 3) 배치 기록의 맨 위가
   `SUCCESS` 또는 `PARTIAL_SUCCESS` 인지 확인
3. 일간 점검 화면 상단의 수집 상태 안내가 사라졌는지 확인

`PARTIAL_SUCCESS` 는 실패가 아니다. 통합기 일부만 실패한 것이고, 성공한 통합기의
변경은 그대로 반영된다. 어느 통합기가 빠졌는지는 화면 상단 안내와 3) 의 사유에 나온다.
