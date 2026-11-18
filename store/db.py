"""数据库连接与迁移。

迁移用顺序 SQL 文件（`migrations/NNNN_*.sql`）+ `schema_version` 表，不引 Alembic：
schema 变动不频繁，一个版本号一张表足够，少一个必须维护的依赖。

SQLite 的三个 pragma 必须每个连接都设，不能只设一次：
- `journal_mode=WAL`：读写不互相阻塞，worker 写的同时 API 能读；
- `foreign_keys=ON`：SQLite 默认**不**检查外键，不开这个，stage_run 指向
  不存在的 run 也能插进去，链条断了还查不出来；
- `busy_timeout=5000`：API 和 worker 同时写时等 5 秒而不是立刻报 locked。
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "workflow.db"
MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def _apply_pragmas(dbapi_conn, _record) -> None:
    cur = dbapi_conn.cursor()
    try:
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=5000")
    finally:
        cur.close()


def create_db_engine(db_path: str | Path = DEFAULT_DB_PATH, echo: bool = False) -> Engine:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{path}", future=True, echo=echo)
    event.listen(engine, "connect", _apply_pragmas)
    return engine


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    # expire_on_commit=False：提交后还要拿对象去渲染报告，不能一提交就过期
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


def _applied_versions(engine: Engine) -> set[int]:
    with engine.connect() as conn:
        exists = conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
        ).fetchone()
        if not exists:
            return set()
        rows = conn.exec_driver_sql("SELECT version FROM schema_version").fetchall()
    return {int(r[0]) for r in rows}


def migrate(engine: Engine) -> list[int]:
    """执行尚未应用的迁移，返回本次应用的版本号列表。

    版本号由**执行器**记账，不靠 SQL 文件自己写 `schema_version`：
    靠文件自觉的话，漏写一次就会每次打开都重放一遍，到 ALTER TABLE 直接炸。

    每个文件整体成功或整体失败：`executescript` 会先隐式 COMMIT，
    所以迁移文件必须保证可重复执行（`CREATE TABLE IF NOT EXISTS` 之类）。
    """
    applied = _applied_versions(engine)
    done: list[int] = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        try:
            version = int(path.name.split("_", 1)[0])
        except ValueError:
            continue
        if version in applied:
            continue
        raw = engine.raw_connection()
        try:
            raw.executescript(path.read_text(encoding="utf-8"))
            raw.execute(
                "INSERT OR IGNORE INTO schema_version(version, applied_at, note) "
                "VALUES (?, datetime('now'), ?)",
                (version, path.name),
            )
            raw.commit()
        finally:
            raw.close()
        done.append(version)
    return done


def open_db(db_path: str | Path = DEFAULT_DB_PATH) -> tuple[Engine, sessionmaker[Session], list[int]]:
    """建引擎、跑迁移，一步到位。返回 (engine, session_factory, 本次应用的版本)。"""
    engine = create_db_engine(db_path)
    applied = migrate(engine)
    return engine, create_session_factory(engine), applied
