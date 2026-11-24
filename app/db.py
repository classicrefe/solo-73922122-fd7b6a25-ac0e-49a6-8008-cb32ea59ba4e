"""SQLite 连接与事务工具。"""
import os
import sqlite3
from contextlib import contextmanager

from flask import current_app, g

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  k TEXT PRIMARY KEY,
  v TEXT
);
CREATE TABLE IF NOT EXISTS rows_a (
  row_no INTEGER PRIMARY KEY,
  data   TEXT NOT NULL          -- JSON：{列名: 值}
);
CREATE TABLE IF NOT EXISTS rows_b (
  row_no INTEGER PRIMARY KEY,
  data   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS keys (
  id         INTEGER PRIMARY KEY,
  cols       TEXT NOT NULL,     -- JSON：键列名列表
  compatible INTEGER NOT NULL DEFAULT 1   -- 是否被当前判定集兼容（1 合规 / 0 失效）
);
-- 键值投影：每个候选键 × 每份表 × 每行 的键值散列。
-- 复核变动后仅凭本表重算候选键合规性，不重扫 rows_a / rows_b。
CREATE TABLE IF NOT EXISTS projections (
  key_id INTEGER NOT NULL,
  ds     TEXT NOT NULL,         -- 'A' | 'B'
  row_no INTEGER NOT NULL,
  h      INTEGER NOT NULL,      -- 键值组合的 64 位散列
  PRIMARY KEY (key_id, ds, row_no)
);
CREATE TABLE IF NOT EXISTS judgments (
  id         INTEGER PRIMARY KEY,
  row_a      INTEGER NOT NULL,  -- 旧表行号（数据行，从 1 计）
  row_b      INTEGER NOT NULL,  -- 新表行号
  typ        TEXT NOT NULL CHECK (typ IN ('same', 'diff')),  -- same=同次 diff=异次
  created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_judgments_pair ON judgments(row_a, row_b);
"""


def connect(path):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def init_db(path):
    conn = connect(path)
    try:
        with conn:
            conn.executescript(SCHEMA)
    finally:
        conn.close()


def get_db():
    if "db" not in g:
        g.db = connect(current_app.config["DATABASE"])
    return g.db


def close_db(exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


@contextmanager
def transaction(conn):
    """写事务：任一环节失败整体回滚——判定、候选键状态、供数状态同生共死。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()
