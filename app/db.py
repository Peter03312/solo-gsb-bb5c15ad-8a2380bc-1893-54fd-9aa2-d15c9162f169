"""SQLite 存取层：建表与连接管理。"""
import sqlite3
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
    device_id   TEXT NOT NULL,
    event_no    TEXT NOT NULL,
    sampled_at  TEXT NOT NULL,          -- UTC ISO-8601，固定微秒宽度，可直接排序
    temperature REAL NOT NULL,
    revoked     INTEGER NOT NULL DEFAULT 0,  -- 撤销采用墓碑标记，保留审计痕迹
    revision    INTEGER NOT NULL DEFAULT 0,  -- 修订号：每次变更（直接操作或争议执行）递增
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (device_id, event_no)
);

CREATE TABLE IF NOT EXISTS disputes (
    device_id          TEXT NOT NULL,
    dispute_no         TEXT NOT NULL,
    event_no           TEXT NOT NULL,
    action             TEXT NOT NULL,        -- correct | revoke
    target_sampled_at  TEXT,                 -- action=correct 时的更正目标
    target_temperature REAL,
    reason             TEXT NOT NULL,        -- 申请原因（非空，全程保留）
    status             TEXT NOT NULL,        -- pending | approved | rejected | expired
    -- 申请时冻结的读数快照与修订号（批准时据此核对）
    frozen_sampled_at  TEXT NOT NULL,
    frozen_temperature REAL NOT NULL,
    frozen_revoked     INTEGER NOT NULL,
    frozen_revision    INTEGER NOT NULL,
    -- 决定与执行结果
    decision           TEXT,                 -- approve | reject
    decided_at         TEXT,
    expire_reason      TEXT,                 -- 过期原因（批准时修订号已变化等）
    changes_json       TEXT,                 -- 决定当次的区间差异（JSON）
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    PRIMARY KEY (device_id, dispute_no)
);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """对老库做增量迁移（幂等）：readings 补充 revision 修订号列。"""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(readings)")}
    if "revision" not in cols:
        conn.execute(
            "ALTER TABLE readings ADD COLUMN revision INTEGER NOT NULL DEFAULT 0"
        )


def init_db(path: str) -> None:
    """初始化数据库 schema（幂等），服务启动时调用。"""
    conn = sqlite3.connect(path)
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.commit()
    finally:
        conn.close()


@contextmanager
def connect(path: str):
    """打开一个连接并确保 schema 存在（幂等，便于任意入口直接使用）。"""
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.executescript(SCHEMA)
        _migrate(conn)
        yield conn
    finally:
        conn.close()
