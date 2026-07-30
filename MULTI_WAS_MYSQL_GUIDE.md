# 다중 WAS · MySQL 전환 가이드

여러 WAS가 하나의 DB를 공유하는 구성으로 전환할 때의 절차와 주의사항이다.

```text
WAS #1 ┐
WAS #2 ┼─→ MySQL (assetdb)
WAS #3 ┘        ↑
          일일 배치 (WAS 중 1대에만 등록)
```

## 1. 엔진 선택

`database.engine` 값으로 결정한다. 기본값은 `sqlite`이며 데모·단일 호스트 설치는 그대로 동작한다.

| 값 | 용도 |
|---|---|
| `sqlite` | 단일 호스트, 설치 검증, 로컬 개발, 테스트 |
| `mysql` | **여러 WAS가 하나의 DB를 공유하는 운영 구성** |

`config/app_config.local.yaml`:

```yaml
database:
  engine: mysql
  mysql:
    host: <MYSQL_HOST_OR_VIP>
    port: 3306
    database: assetdb
    user: <ASSET_DB_USER>
    charset: utf8mb4
    connect_timeout_seconds: 10
```

비밀번호는 파일에 쓰지 않고 `.env` 또는 프로세스 환경변수로 주입한다. 환경변수가 항상 파일값을 덮어쓴다.

```text
ASSET_DB_ENGINE=mysql
MYSQL_HOST=<MYSQL_HOST_OR_VIP>
MYSQL_PORT=3306
MYSQL_DATABASE=assetdb
MYSQL_USER=<ASSET_DB_USER>
MYSQL_PASSWORD=<SET_IN_LOCAL_ENVIRONMENT>
```

## 2. 드라이버 설치

```bat
.venv\Scripts\python.exe -m pip install --no-index --find-links wheels -r requirements-mysql.txt
```

PyMySQL을 사용한다. 순수 파이썬이라 폐쇄망에 wheel 하나만 반입하면 되고 컴파일러가 필요 없다. `mysql-connector-python`이 이미 설치되어 있으면 그것도 인식한다.

## 3. DB와 계정 준비

```sql
CREATE DATABASE assetdb CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER '<ASSET_DB_USER>'@'<WAS_SUBNET>' IDENTIFIED BY '<PASSWORD>';
GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, INDEX, REFERENCES ON assetdb.* TO '<ASSET_DB_USER>'@'<WAS_SUBNET>';
```

`CREATE`와 `INDEX`가 필요한 이유는 애플리케이션이 기동 시 `CREATE TABLE IF NOT EXISTS`로 스키마를 맞추기 때문이다. DBA가 DDL을 직접 관리한다면 `asset_sync/db/schema_mysql.sql`을 전달하고 `SELECT, INSERT, UPDATE, DELETE`만 부여해도 된다.

스키마 생성:

```bat
.venv\Scripts\python.exe scripts\initialize_db.py
```

## 4. 기존 SQLite 데이터 이관

```bat
.venv\Scripts\python.exe scripts\migrate_sqlite_to_mysql.py --check
.venv\Scripts\python.exe scripts\migrate_sqlite_to_mysql.py
```

`--check`는 쓰기 없이 양쪽 건수만 비교한다. 본 실행은 외래키 순서대로 복사하고 끝에 건수를 재확인하며, 대상에 데이터가 있으면 중단한다(`--truncate`로 강제 가능). 이관 후 다시 `--check`로 모든 테이블이 일치하는지 확인한다.

## 5. SQLite와 MySQL의 차이 (이미 처리된 항목)

애플리케이션 SQL은 SQLite 표기(`?` 파라미터, `INSERT OR IGNORE`)로 한 번만 작성하고 `asset_sync/db/dialects.py`가 실행 시점에 변환한다. 스키마는 두 파일로 분리되어 있다.

| SQLite | MySQL | 처리 방식 |
|---|---|---|
| `?` 파라미터 | `%s` | 문자열·주석을 건너뛰는 스캐너로 변환 |
| `INSERT OR IGNORE` | `INSERT IGNORE` | 문장 선두 패턴만 치환 |
| `CREATE INDEX IF NOT EXISTS` | 미지원 | `CREATE TABLE` 안에 인라인 정의 |
| 부분 유니크 인덱스 `WHERE active_yn=1` | 미지원 | 비활성 시 NULL이 되는 생성 컬럼 + 유니크 인덱스 |
| `IFNULL(...)` 표현식 인덱스 | 8.0.13+ 만 지원 | 생성 컬럼으로 대체 (5.7 호환) |
| `TEXT ... DEFAULT '[]'` | 불가 | 작은 JSON은 `VARCHAR`, 큰 것은 `TEXT`(판독부가 `or "{}"` 처리) |
| `PRAGMA` | 없음 | MySQL 경로에서 사용하지 않음 |
| `datetime('now')` | `NOW()` | 파이썬이 ISO 문자열을 바인딩 |

**`schema.sql`과 `schema_mysql.sql`은 함께 수정해야 한다.** 테이블 목록이 어긋나면 `tests/test_database_engines.py`가 실패한다.

## 6. 일일 배치는 1대에서만 실행된다

배치 중복 실행 방지 장치가 파일 잠금에서 **공유 DB 잠금**(`process_lock` 테이블)으로 바뀌었다. 파일 잠금은 같은 호스트만 보호하므로 여러 WAS 환경에서는 두 대가 동시에 07:00 수집을 실행해 스냅샷이 중복될 수 있었다.

- 잠금을 얻지 못한 WAS는 `{"status": "SKIPPED", ...}`를 출력하고 **정상 종료(exit 0)** 한다. 스케줄러가 실패로 보고하지 않는다.
- 잠금에는 리스(기본 4시간)가 있어 배치 프로세스가 죽어도 자동으로 회수된다. `scheduler.batch_lock_seconds`로 조정한다.

작업 스케줄러는 **여러 WAS에 등록해도 안전하다.** 한 대만 실제로 수집하고 나머지는 건너뛴다. 다만 운영 혼선을 줄이려면 1대만 등록하고 나머지는 예비로 두는 편이 낫다.

## 7. DB 외에 반드시 함께 처리할 것

DB를 공유해도 아래는 여전히 WAS별 로컬 상태라 다중화 시 문제가 된다. **MySQL 전환만으로 해결되지 않는다.**

### 7-1. Flask 세션 키

세션 쿠키에 서명하는 키다. 기본값이 코드에 하드코딩된 문자열이므로 **운영 배포 전에 반드시 교체한다.** 값을 알면 세션 위조가 가능하다.

```text
FLASK_SECRET_KEY=<충분히 긴 임의 문자열>
```

통합 웹을 여러 대로 복제해 앞단에 로드밸런서를 두는 구성이라면 모든 인스턴스가 **같은 값**을 써야 세션이 유지된다. 통합 웹이 한 대이고 뒤에 도메인 WAS를 API로 붙이는 구성이라면 값 일치는 필요 없고 교체만 하면 된다.

### 7-2. 계정 (DB로 이관 완료)

계정은 `app_user` 테이블에 저장되며 모든 WAS가 같은 목록을 본다. 비밀번호는 Werkzeug 해시로만 저장한다.

최초 기동 시 동작 순서:

```text
1. app_user 가 비어 있고 data/users.json 이 있으면 → 파일에서 이관
   (평문 비밀번호는 이관 시점에 해시로 변환되며 기존 비밀번호로 계속 로그인된다)
2. 그래도 비어 있으면 → 기본 계정 admin / user 생성
3. 계정이 하나라도 있으면 → 아무것도 하지 않음 (멱등)
```

관리 화면에서 생성·권한 변경·비밀번호 변경·사용 중지·삭제를 할 수 있고, 모든 변경은 `audit_log`에 `target_type='app_user'`로 남는다. **활성 관리자가 하나뿐이면 삭제·비활성화·권한 강등이 거부된다** — 아무도 관리 화면에 들어갈 수 없게 되는 상황을 막기 위한 것이다.

**후속 조치 2건:**

1. `data/users.json`은 저장소에 추적되어 있고 기본 계정의 비밀번호 해시가 들어 있다. 이관이 정상 완료된 것을 확인한 뒤 `git rm --cached data/users.json` + `.gitignore` 추가로 정리한다. 이관 전에 지우면 계정을 파일에서 가져올 수 없으므로 순서를 지켜야 한다.
2. 기본 계정 `admin` / `user`의 초기 비밀번호는 문서에 공개된 값이다. 운영 배포 전에 반드시 변경한다.

### 7-3. 남은 JSON 파일 상태 (미해결)

```text
data/mappings.json   보고서 컬럼 매핑
data/history.json    보고 이력
```

레거시 보고 기능이 로컬 디스크에 쓰므로 한 WAS에서 수정한 내용이 다른 WAS에서 보이지 않는다. 임시 대응은 공유 스토리지에 두는 것이고, 정식 해결은 DB 테이블로 옮기는 것이다. 통합 웹 한 대만 이 기능을 담당한다면 문제되지 않는다.

### 7-4. 업로드·산출물 디렉터리

```text
uploads/  outputs/  data/incoming/  data/archive/  data/export/  data/temp/
```

한 WAS에 업로드한 파일은 다른 WAS에서 보이지 않는다. 업로드-분석-다운로드가 한 요청 흐름에서 끝나면 문제되지 않지만, 다음 두 경우는 공유 스토리지가 필요하다.

- vCenter/ITSM 원본 파일을 나중에 다시 조회하는 `FILE_ONLY` 모드
- 생성한 XLSX를 다른 요청에서 내려받는 흐름

로드밸런서에 세션 고정(sticky session)을 걸면 대부분 회피되지만, 배치가 만든 보관본은 배치를 실행한 WAS에만 남는다.

### 7-5. 스키마 생성 경합

여러 WAS가 동시에 기동하면 `CREATE TABLE IF NOT EXISTS`가 동시에 실행된다. 멱등 구문이라 데이터가 깨지지는 않지만, 동시에 같은 테이블을 만들면 한쪽이 일시적 오류로 기동에 실패할 수 있다. 최초 배포 시에는 `scripts\initialize_db.py`를 **1회 먼저 실행**한 뒤 WAS를 기동한다.

## 8. 검증

```bat
.venv\Scripts\python.exe scripts\verify_installation.py
```

`database_engine`이 `mysql`, `database`가 `mysql:<host>:<port>/<db>`로 나오는지 확인한다. 웹에서는 `/api/health`가 같은 정보를 준다.

```json
{"status": "UP", "engine": "mysql", "database": "mysql:10.0.0.9:3306/assetdb"}
```

전체 흐름 검증:

```bat
.venv\Scripts\python.exe jobs\daily_batch.py --demo
```

두 번째 WAS에서 같은 명령을 동시에 실행하면 `SKIPPED`가 나오는 것이 정상이다.
