"""SQLite 存储层：连接、建表、公共助手。

设计要点：
- 所有写操作（批次提交、日发布）都在显式 BEGIN IMMEDIATE ... COMMIT 事务内完成，
  失败即 ROLLBACK，保证不会出现"半份结果"。
- meta.event_set_version：每提交一个"有实际变更"的批次 +1，标识已提交事件集版本。
- day_versions：每个 UTC 日的数据版本，批次提交时按受影响日期 +1，
  用于判断某日发布是否已过期。
"""
import os
import sqlite3
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- 已提交事件集（唯一事实来源）
CREATE TABLE IF NOT EXISTS events (
  event_id    TEXT PRIMARY KEY,
  revision    INTEGER NOT NULL CHECK (revision > 0),
  card_id     TEXT NOT NULL,
  ts_utc      TEXT NOT NULL,          -- 归一化 ISO8601，如 2026-11-16T23:30:00Z
  ts_epoch_us INTEGER NOT NULL,       -- UTC 微秒时间戳，用于排序与"同刻"判定
  station     TEXT NOT NULL,
  direction   TEXT NOT NULL CHECK (direction IN ('in','out')),
  batch_id    INTEGER NOT NULL,       -- 写入当前版本的批次
  updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_card_ts ON events(card_id, ts_epoch_us);

-- 上传批次（含失败批次，便于审计；失败批次不改动事件集）
CREATE TABLE IF NOT EXISTS batches (
  batch_id          INTEGER PRIMARY KEY AUTOINCREMENT,
  uploaded_at       TEXT NOT NULL,
  filename          TEXT,
  status            TEXT NOT NULL CHECK (status IN ('committed','failed')),
  rows_total        INTEGER NOT NULL DEFAULT 0,
  rows_new          INTEGER NOT NULL DEFAULT 0,
  rows_updated      INTEGER NOT NULL DEFAULT 0,
  rows_unchanged    INTEGER NOT NULL DEFAULT 0,
  rows_ignored      INTEGER NOT NULL DEFAULT 0,
  event_set_version INTEGER,          -- 提交后的事件集版本；失败批次为 NULL
  error             TEXT,
  details           TEXT              -- JSON：逐事件处理明细
);

-- 每个批次影响到的 UTC 日（事件自身所在日及其前一日，因配对窗口最长 4 小时）
CREATE TABLE IF NOT EXISTS batch_affected_days (
  batch_id INTEGER NOT NULL,
  day      TEXT NOT NULL,
  PRIMARY KEY (batch_id, day)
);

CREATE TABLE IF NOT EXISTS day_versions (
  day     TEXT PRIMARY KEY,
  version INTEGER NOT NULL
);

-- 发布快照：某 UTC 日某时刻的行程/异常结果，发布后不可变
CREATE TABLE IF NOT EXISTS publications (
  pub_id            INTEGER PRIMARY KEY AUTOINCREMENT,
  day               TEXT NOT NULL,
  created_at        TEXT NOT NULL,
  event_set_version INTEGER NOT NULL, -- 基于哪个已提交事件集
  day_version       INTEGER NOT NULL, -- 发布时该日的数据版本
  status            TEXT NOT NULL CHECK (status IN ('current','superseded')),
  trip_count        INTEGER NOT NULL,
  anomaly_count     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS publication_trips (
  pub_id         INTEGER NOT NULL,
  card_id        TEXT NOT NULL,
  entry_event_id TEXT NOT NULL,
  exit_event_id  TEXT NOT NULL,
  entry_station  TEXT NOT NULL,
  exit_station   TEXT NOT NULL,
  entry_ts       TEXT NOT NULL,
  exit_ts        TEXT NOT NULL,
  duration_min   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ptrips_pub ON publication_trips(pub_id);

CREATE TABLE IF NOT EXISTS publication_anomalies (
  pub_id    INTEGER NOT NULL,
  card_id   TEXT NOT NULL,
  event_id  TEXT NOT NULL,
  ts_utc    TEXT NOT NULL,
  station   TEXT NOT NULL,
  direction TEXT NOT NULL,
  reason    TEXT NOT NULL            -- dangling_entry | orphan_exit
);
CREATE INDEX IF NOT EXISTS idx_panom_pub ON publication_anomalies(pub_id);
"""


def connect(db_path):
    """新建连接。isolation_level=None 为自动提交，写事务用显式 BEGIN IMMEDIATE。"""
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn):
    conn.executescript(SCHEMA)
    conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES('event_set_version', '0')")


def get_esv(conn):
    """当前已提交事件集版本。"""
    return int(conn.execute("SELECT value FROM meta WHERE key='event_set_version'").fetchone()["value"])


def get_day_version(conn, day):
    row = conn.execute("SELECT version FROM day_versions WHERE day=?", (day,)).fetchone()
    return row["version"] if row else 0


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ensure_parent_dir(path):
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
