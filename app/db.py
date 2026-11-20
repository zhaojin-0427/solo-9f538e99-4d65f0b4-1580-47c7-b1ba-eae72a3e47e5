"""SQLite 存储层：schema 初始化与仓储函数。

派生结论（分组、可行年份）一律不落库，每次查询都由原始记录现场求解，
因此“每次查询及重启后，分组和年代均来自同一次已完成的提交”由原始记录的
事务原子性天然保证。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Any, Iterator, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sites (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    name    TEXT NOT NULL UNIQUE,
    created TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS strata (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id  INTEGER NOT NULL REFERENCES sites(id),
    trench   TEXT NOT NULL,
    layer_no TEXT NOT NULL,
    note     TEXT,
    created  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (site_id, trench, layer_no)
);

CREATE TABLE IF NOT EXISTS stratum_intervals (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    stratum_id INTEGER NOT NULL REFERENCES strata(id) ON DELETE CASCADE,
    year_from  INTEGER NOT NULL,
    year_to    INTEGER NOT NULL,
    CHECK (year_from <= year_to)
);

CREATE TABLE IF NOT EXISTS correlations (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id      INTEGER NOT NULL REFERENCES sites(id),
    stratum_a_id INTEGER NOT NULL REFERENCES strata(id),
    stratum_b_id INTEGER NOT NULL REFERENCES strata(id),
    created      TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (stratum_a_id < stratum_b_id),
    UNIQUE (site_id, stratum_a_id, stratum_b_id)
);

CREATE TABLE IF NOT EXISTS superpositions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id      INTEGER NOT NULL REFERENCES sites(id),
    lower_id     INTEGER NOT NULL REFERENCES strata(id),
    upper_id     INTEGER NOT NULL REFERENCES strata(id),
    created      TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (lower_id <> upper_id),
    UNIQUE (site_id, lower_id, upper_id)
);

-- 土袋：一个遗址内袋号唯一；原始体积为可累计取用的上限（毫升）。
CREATE TABLE IF NOT EXISTS sample_bags (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id    INTEGER NOT NULL REFERENCES sites(id),
    stratum_id INTEGER NOT NULL REFERENCES strata(id),
    bag_no     TEXT NOT NULL,
    volume_ml  REAL NOT NULL CHECK (volume_ml > 0),
    note       TEXT,
    created    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (site_id, bag_no)
);

-- 浮选批：一个遗址内批号唯一；籽粒总数为整批筛出量。
CREATE TABLE IF NOT EXISTS flotation_batches (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id    INTEGER NOT NULL REFERENCES sites(id),
    batch_no   TEXT NOT NULL,
    seed_count INTEGER NOT NULL CHECK (seed_count >= 0),
    note       TEXT,
    created    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (site_id, batch_no)
);

-- 一批浮选由若干袋土组成，每袋在同一批中只登记一次取用量。
CREATE TABLE IF NOT EXISTS flotation_items (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES flotation_batches(id) ON DELETE CASCADE,
    bag_id   INTEGER NOT NULL REFERENCES sample_bags(id),
    used_ml  REAL NOT NULL CHECK (used_ml > 0),
    UNIQUE (batch_id, bag_id)
);

CREATE INDEX IF NOT EXISTS idx_strata_site ON strata(site_id);
CREATE INDEX IF NOT EXISTS idx_intervals_stratum ON stratum_intervals(stratum_id);
CREATE INDEX IF NOT EXISTS idx_corr_site ON correlations(site_id);
CREATE INDEX IF NOT EXISTS idx_sup_site ON superpositions(site_id);
CREATE INDEX IF NOT EXISTS idx_bags_site ON sample_bags(site_id);
CREATE INDEX IF NOT EXISTS idx_bags_stratum ON sample_bags(stratum_id);
CREATE INDEX IF NOT EXISTS idx_batches_site ON flotation_batches(site_id);
CREATE INDEX IF NOT EXISTS idx_items_batch ON flotation_items(batch_id);
CREATE INDEX IF NOT EXISTS idx_items_bag ON flotation_items(bag_id);
"""


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES ('version', '0')"
    )
    conn.commit()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


@contextmanager
def read_snapshot(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """在单个读事务中完成多条查询，保证读到同一个已提交快照。

    已处于事务中（例如写接口内部）时直接复用当前事务，不重复 BEGIN。
    """
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN")
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


# ---------- meta / 版本号 ----------

def get_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key = 'version'").fetchone()
    return int(row["value"])


def bump_version(conn: sqlite3.Connection) -> int:
    conn.execute(
        "UPDATE meta SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT) "
        "WHERE key = 'version'"
    )
    return get_version(conn)


# ---------- 遗址 ----------

def create_site(conn: sqlite3.Connection, name: str) -> sqlite3.Row:
    cur = conn.execute("INSERT INTO sites(name) VALUES (?)", (name,))
    return conn.execute("SELECT * FROM sites WHERE id = ?", (cur.lastrowid,)).fetchone()


def get_site(conn: sqlite3.Connection, site_id: int) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM sites WHERE id = ?", (site_id,)).fetchone()


def get_site_by_name(conn: sqlite3.Connection, name: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM sites WHERE name = ?", (name,)).fetchone()


def list_sites(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM sites ORDER BY id").fetchall()


# ---------- 层位 ----------

def count_strata(conn: sqlite3.Connection, site_id: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS c FROM strata WHERE site_id = ?", (site_id,)
    ).fetchone()["c"]


def get_stratum(conn: sqlite3.Connection, stratum_id: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM strata WHERE id = ?", (stratum_id,)
    ).fetchone()


def find_stratum(
    conn: sqlite3.Connection, site_id: int, trench: str, layer_no: str
) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM strata WHERE site_id = ? AND trench = ? AND layer_no = ?",
        (site_id, trench, layer_no),
    ).fetchone()


def create_stratum(
    conn: sqlite3.Connection,
    site_id: int,
    trench: str,
    layer_no: str,
    note: Optional[str],
    intervals: list[tuple[int, int]],
) -> sqlite3.Row:
    cur = conn.execute(
        "INSERT INTO strata(site_id, trench, layer_no, note) VALUES (?, ?, ?, ?)",
        (site_id, trench, layer_no, note),
    )
    stratum_id = cur.lastrowid
    conn.executemany(
        "INSERT INTO stratum_intervals(stratum_id, year_from, year_to) "
        "VALUES (?, ?, ?)",
        [(stratum_id, lo, hi) for lo, hi in intervals],
    )
    return conn.execute(
        "SELECT * FROM strata WHERE id = ?", (stratum_id,)
    ).fetchone()


def list_strata(conn: sqlite3.Connection, site_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM strata WHERE site_id = ? ORDER BY id", (site_id,)
    ).fetchall()


def get_intervals(
    conn: sqlite3.Connection, stratum_id: int
) -> list[tuple[int, int]]:
    rows = conn.execute(
        "SELECT year_from, year_to FROM stratum_intervals "
        "WHERE stratum_id = ? ORDER BY year_from, year_to",
        (stratum_id,),
    ).fetchall()
    return [(r["year_from"], r["year_to"]) for r in rows]


# ---------- 对接 ----------

def create_correlation(
    conn: sqlite3.Connection,
    site_id: int,
    stratum_a_id: int,
    stratum_b_id: int,
) -> sqlite3.Row:
    lo, hi = sorted((stratum_a_id, stratum_b_id))
    cur = conn.execute(
        "INSERT INTO correlations(site_id, stratum_a_id, stratum_b_id) "
        "VALUES (?, ?, ?)",
        (site_id, lo, hi),
    )
    return conn.execute(
        "SELECT * FROM correlations WHERE id = ?", (cur.lastrowid,)
    ).fetchone()


def find_correlation(
    conn: sqlite3.Connection, stratum_a_id: int, stratum_b_id: int
) -> Optional[sqlite3.Row]:
    lo, hi = sorted((stratum_a_id, stratum_b_id))
    return conn.execute(
        "SELECT * FROM correlations WHERE stratum_a_id = ? AND stratum_b_id = ?",
        (lo, hi),
    ).fetchone()


def get_correlation(
    conn: sqlite3.Connection, correlation_id: int
) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM correlations WHERE id = ?", (correlation_id,)
    ).fetchone()


def list_correlations(
    conn: sqlite3.Connection, site_id: int
) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM correlations WHERE site_id = ? ORDER BY id", (site_id,)
    ).fetchall()


def delete_correlation(conn: sqlite3.Connection, correlation_id: int) -> None:
    conn.execute("DELETE FROM correlations WHERE id = ?", (correlation_id,))


# ---------- 叠压 ----------

def create_superposition(
    conn: sqlite3.Connection,
    site_id: int,
    lower_id: int,
    upper_id: int,
) -> sqlite3.Row:
    cur = conn.execute(
        "INSERT INTO superpositions(site_id, lower_id, upper_id) VALUES (?, ?, ?)",
        (site_id, lower_id, upper_id),
    )
    return conn.execute(
        "SELECT * FROM superpositions WHERE id = ?", (cur.lastrowid,)
    ).fetchone()


def find_superposition(
    conn: sqlite3.Connection, lower_id: int, upper_id: int
) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM superpositions WHERE lower_id = ? AND upper_id = ?",
        (lower_id, upper_id),
    ).fetchone()


def get_superposition(
    conn: sqlite3.Connection, superposition_id: int
) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM superpositions WHERE id = ?", (superposition_id,)
    ).fetchone()


def list_superpositions(
    conn: sqlite3.Connection, site_id: int
) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM superpositions WHERE site_id = ? ORDER BY id", (site_id,)
    ).fetchall()


def delete_superposition(conn: sqlite3.Connection, superposition_id: int) -> None:
    conn.execute("DELETE FROM superpositions WHERE id = ?", (superposition_id,))


# ---------- 土袋 ----------

def create_sample_bag(
    conn: sqlite3.Connection,
    site_id: int,
    stratum_id: int,
    bag_no: str,
    volume_ml: float,
    note: Optional[str],
) -> sqlite3.Row:
    cur = conn.execute(
        "INSERT INTO sample_bags(site_id, stratum_id, bag_no, volume_ml, note) "
        "VALUES (?, ?, ?, ?, ?)",
        (site_id, stratum_id, bag_no, volume_ml, note),
    )
    return conn.execute(
        "SELECT * FROM sample_bags WHERE id = ?", (cur.lastrowid,)
    ).fetchone()


def find_sample_bag(
    conn: sqlite3.Connection, site_id: int, bag_no: str
) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM sample_bags WHERE site_id = ? AND bag_no = ?",
        (site_id, bag_no),
    ).fetchone()


def get_sample_bag(
    conn: sqlite3.Connection, bag_id: int
) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM sample_bags WHERE id = ?", (bag_id,)
    ).fetchone()


def list_sample_bags(
    conn: sqlite3.Connection, site_id: int
) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM sample_bags WHERE site_id = ? ORDER BY id", (site_id,)
    ).fetchall()


def total_used_ml(
    conn: sqlite3.Connection, bag_id: int, exclude_batch_id: Optional[int] = None
) -> float:
    """某袋在全部浮选批中已占用的毫升数（可按批排除，用于原样重发判定）。"""
    sql = (
        "SELECT COALESCE(SUM(used_ml), 0) AS total FROM flotation_items "
        "WHERE bag_id = ?"
    )
    params: list[Any] = [bag_id]
    if exclude_batch_id is not None:
        sql += " AND batch_id <> ?"
        params.append(exclude_batch_id)
    return float(conn.execute(sql, params).fetchone()["total"])


def bag_usage_rows(
    conn: sqlite3.Connection, bag_ids: list[int]
) -> dict[int, dict[str, Any]]:
    """批量统计各袋的累计取用量与批次数。"""
    if not bag_ids:
        return {}
    marks = ", ".join("?" for _ in bag_ids)
    rows = conn.execute(
        f"SELECT bag_id, COALESCE(SUM(used_ml), 0) AS used_ml, "
        f"COUNT(*) AS batch_count FROM flotation_items "
        f"WHERE bag_id IN ({marks}) GROUP BY bag_id",
        bag_ids,
    ).fetchall()
    return {
        r["bag_id"]: {
            "used_ml": float(r["used_ml"]),
            "batch_count": int(r["batch_count"]),
        }
        for r in rows
    }


# ---------- 浮选批 ----------

def create_batch(
    conn: sqlite3.Connection,
    site_id: int,
    batch_no: str,
    seed_count: int,
    note: Optional[str],
) -> sqlite3.Row:
    cur = conn.execute(
        "INSERT INTO flotation_batches(site_id, batch_no, seed_count, note) "
        "VALUES (?, ?, ?, ?)",
        (site_id, batch_no, seed_count, note),
    )
    return conn.execute(
        "SELECT * FROM flotation_batches WHERE id = ?", (cur.lastrowid,)
    ).fetchone()


def find_batch(
    conn: sqlite3.Connection, site_id: int, batch_no: str
) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM flotation_batches WHERE site_id = ? AND batch_no = ?",
        (site_id, batch_no),
    ).fetchone()


def get_batch(conn: sqlite3.Connection, batch_id: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM flotation_batches WHERE id = ?", (batch_id,)
    ).fetchone()


def list_batches(conn: sqlite3.Connection, site_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM flotation_batches WHERE site_id = ? ORDER BY id", (site_id,)
    ).fetchall()


def delete_batch(conn: sqlite3.Connection, batch_id: int) -> None:
    """撤销浮选批：批与全部取用明细一并删除，土袋余量随之恢复。"""
    conn.execute("DELETE FROM flotation_items WHERE batch_id = ?", (batch_id,))
    conn.execute("DELETE FROM flotation_batches WHERE id = ?", (batch_id,))


def add_batch_item(
    conn: sqlite3.Connection, batch_id: int, bag_id: int, used_ml: float
) -> None:
    conn.execute(
        "INSERT INTO flotation_items(batch_id, bag_id, used_ml) VALUES (?, ?, ?)",
        (batch_id, bag_id, used_ml),
    )


def list_batch_items(
    conn: sqlite3.Connection, batch_id: int
) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT i.*, g.bag_no, g.volume_ml, g.stratum_id, "
        "       s.trench, s.layer_no "
        "FROM flotation_items i "
        "JOIN sample_bags g ON g.id = i.bag_id "
        "JOIN strata s ON s.id = g.stratum_id "
        "WHERE i.batch_id = ? ORDER BY i.id",
        (batch_id,),
    ).fetchall()


def items_for_site(
    conn: sqlite3.Connection, site_id: int
) -> list[sqlite3.Row]:
    """某遗址全部批次的取用明细（含批号、土袋与地层信息）。"""
    return conn.execute(
        "SELECT i.batch_id, i.bag_id, i.used_ml, "
        "       b.batch_no, b.seed_count, "
        "       g.bag_no, g.volume_ml, g.stratum_id, "
        "       s.trench, s.layer_no "
        "FROM flotation_items i "
        "JOIN flotation_batches b ON b.id = i.batch_id "
        "JOIN sample_bags g ON g.id = i.bag_id "
        "JOIN strata s ON s.id = g.stratum_id "
        "WHERE b.site_id = ? ORDER BY i.batch_id, i.id",
        (site_id,),
    ).fetchall()


def items_for_site(
    conn: sqlite3.Connection, site_id: int
) -> list[sqlite3.Row]:
    """某遗址全部批次的取用明细（含批号、土袋与地层信息）。"""
    return conn.execute(
        "SELECT i.batch_id, i.bag_id, i.used_ml, "
        "       b.batch_no, b.seed_count, "
        "       g.bag_no, g.volume_ml, g.stratum_id, "
        "       s.trench, s.layer_no "
        "FROM flotation_items i "
        "JOIN flotation_batches b ON b.id = i.batch_id "
        "JOIN sample_bags g ON g.id = i.bag_id "
        "JOIN strata s ON s.id = g.stratum_id "
        "WHERE b.site_id = ? ORDER BY i.batch_id, i.id",
        (site_id,),
    ).fetchall()
