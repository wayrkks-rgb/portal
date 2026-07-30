# 대메뉴(모듈)별 DB 파일

여러 담당자가 같은 저장소를 branch 로 나눠 쓰고 하나의 DB 를 공유한다. 공용
`schema.sql` / `migrations.py` 를 각자 고치면 병합할 때마다 같은 줄에서 충돌하고,
최악의 경우 한쪽 변경이 조용히 사라진다. 그래서 **자기 파일만 이 폴더에 추가**한다.
공용 파일은 건드리지 않는다.

## 1. 파일 이름

| 파일 | 내용 |
| --- | --- |
| `<module_id>.sql` | SQLite 용 테이블/인덱스 (개발·데모·테스트) |
| `<module_id>.mysql.sql` | MySQL 용 같은 테이블/인덱스 (운영) |
| `<module_id>_migrations.py` | 이미 배포된 DB 를 바꾸는 단계 (선택) |

`<module_id>` 는 `config/modules/<module_id>.yaml` 의 모듈 ID 와 같게 쓴다.

**두 SQL 파일은 짝으로 둔다.** 한쪽만 있으면 기동할 때 바로 실패한다. 한쪽만 두면
다른 엔진으로 배포했을 때 테이블이 조용히 없는 상태가 되기 때문이다.

## 2. 테이블 이름 규칙

만드는 객체 이름은 `<module_id>_` 로 시작해야 한다. 인덱스는 `idx_`, `uq_`, `ix_`,
`ux_`, `uniq_`, `fk_` 를 앞에 붙인 형태도 허용한다.

```sql
-- capacity.sql  (module_id = capacity)
CREATE TABLE IF NOT EXISTS capacity_snapshot (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cluster_name TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    cpu_usage_pct REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_capacity_snapshot_time ON capacity_snapshot(captured_at DESC);
```

규칙을 어기면 기동 시점에 무엇이 잘못됐는지 알려주며 멈춘다. 하나의 DB 를 공유하니
이름이 겹치면 서로의 테이블을 덮어쓴다.

## 3. SQL 파일에 쓸 수 있는 것

`CREATE TABLE`, `CREATE INDEX`, `CREATE VIEW`, 그리고 **자기 테이블에 대한**
`INSERT` 만 쓸 수 있다. `ALTER`, `DROP`, `TRUNCATE` 는 막는다. 그건 이미 배포된 DB
에 대한 변경이라 적용 이력이 남아야 하므로 마이그레이션으로 간다.

`CREATE TABLE IF NOT EXISTS` 로 쓴다. 기동할 때마다 실행되기 때문이다. 그래서
**새 컬럼을 참조하는 인덱스를 SQL 파일에 두면 안 된다** — 이미 테이블이 있는 DB 에서는
`CREATE TABLE IF NOT EXISTS` 가 no-op 이라 컬럼 없이 인덱스를 만들려다 실패한다.
컬럼은 `CREATE TABLE` 에 넣고, 인덱스는 마이그레이션에서 만든다.

## 4. 마이그레이션 (이미 배포된 DB 바꾸기)

```python
# capacity_migrations.py
from asset_sync.db.migrations import apply_step, column_exists


def _add_memory_column(conn) -> bool:
    """컬럼과 인덱스를 따로 확인한다.

    새로 만든 DB 는 CREATE TABLE 로 컬럼을 이미 갖지만 인덱스는 없다.
    """
    mysql = conn.dialect.name == "mysql"
    changed = False
    if not column_exists(conn, "capacity_snapshot", "mem_usage_pct"):
        apply_step(conn, "ALTER TABLE capacity_snapshot ADD COLUMN mem_usage_pct REAL NOT NULL DEFAULT 0")
        changed = True
    apply_step(
        conn,
        "CREATE INDEX idx_capacity_snapshot_mem ON capacity_snapshot(mem_usage_pct)"
        if mysql
        else "CREATE INDEX IF NOT EXISTS idx_capacity_snapshot_mem ON capacity_snapshot(mem_usage_pct)",
    )
    return changed


# (이름, 설명, 적용 함수). 함수는 실제로 바꾼 것이 있으면 True 를 돌려준다.
MIGRATIONS = [
    ("add_memory_column", "capacity_snapshot.mem_usage_pct 추가", _add_memory_column),
]
```

지켜야 할 두 가지:

1. **이름은 숫자가 아니다.** 이력은 `schema_migration.name` 에 `capacity/add_memory_column`
   형태로 남는다. 숫자를 쓰면 담당자 A 와 B 가 같은 번호를 쓰고, 병합에서 한쪽만
   남거나 이미 그 번호가 찍힌 DB 에서 다른 쪽이 영구히 스킵된다. 이름은 겹치지 않으므로
   그런 일이 없다. 한 번 배포한 이름은 바꾸지 않는다 (바꾸면 다시 실행된다).
2. **각 함수는 적용 여부를 스스로 확인한다.** 새로 만든 DB 는 최신 스키마를 이미 갖고
   있고, 여러 WAS 가 동시에 기동해 같은 DDL 을 돌릴 수도 있다. `column_exists`,
   `table_exists`, `apply_step` 이 그 용도로 있다. `apply_step` 은 "이미 있음" 계열
   드라이버 오류를 성공으로 취급한다.

SQL 은 SQLite 형식으로 쓴다 (`?` 파라미터, `INSERT OR IGNORE`). MySQL 로는 실행 시점에
자동 변환된다. 다만 DDL 타입은 엔진마다 달라 위 예시처럼 `conn.dialect.name` 으로 갈라야
하는 경우가 있다.

## 5. 적용 순서

기동할 때 `DatabaseManager.initialize()` 가 이 순서로 돈다.

1. 공용 `schema.sql` (또는 `schema_mysql.sql`)
2. 이 폴더의 모듈 SQL 파일 — 모듈 ID 사전순
3. 미적용 마이그레이션 — 코어 먼저, 그 뒤 모듈 파일 이름순

모듈 간 순서는 보장하지 않는다. **다른 모듈의 테이블에 의존하는 단계는 두지 않는다.**
공용 테이블(`app_user`, `audit_log` 등)은 1번에서 이미 만들어져 있으므로 참조해도 된다.

## 6. 확인 방법

```bash
python -c "from asset_sync.db.module_schema import discover_module_schemas; \
print([(f.module_id, len(f.statements)) for f in discover_module_schemas('sqlite')])"
python -c "from asset_sync.db.migrations import all_migrations; \
print([name for name, _, _ in all_migrations()])"
pytest tests/test_module_schema.py tests/test_migrations.py -q
```
