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
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (device_id, event_no)
);
"""


def init_db(path: str) -> None:
    """初始化数据库 schema（幂等），服务启动时调用。"""
    conn = sqlite3.connect(path)
    try:
        conn.executescript(SCHEMA)
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
        yield conn
    finally:
        conn.close()
