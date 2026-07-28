"""SQLite schema, connections, and the FTS5 external-content delete protocol.

`connect()` is on the hot path: the TUI opens one connection and reuses it, but
the CLI opens one per invocation and `retrace s foo` is expected to finish in
~100ms in total. So connect() must not do setup work. It checks
``PRAGMA user_version`` - a single 4-byte header read - and only touches the
schema when that is behind.

Running ``executescript(SCHEMA)`` plus a ``PRAGMA table_info`` migration probe
on every call is the tempting alternative. It is invisible behind 60ms of
interpreter start-up and very visible without it.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from . import config

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS labels(session TEXT PRIMARY KEY, name TEXT NOT NULL, ts TEXT);
CREATE TABLE IF NOT EXISTS excluded(path TEXT PRIMARY KEY, session TEXT, ts TEXT);
CREATE TABLE IF NOT EXISTS files(
    path     TEXT PRIMARY KEY,
    size     INTEGER NOT NULL,
    off      INTEGER NOT NULL,
    -- st_mtime as well as size: a transcript can be rewritten to exactly the
    -- same length, and size alone would call that "untouched"
    mtime    REAL NOT NULL DEFAULT 0,
    project  TEXT,
    sid      TEXT,
    cli_name TEXT
);
CREATE TABLE IF NOT EXISTS msgs(
    id        INTEGER PRIMARY KEY,
    entry_key TEXT NOT NULL UNIQUE,
    source    TEXT NOT NULL,
    session   TEXT NOT NULL,
    project   TEXT,
    role      TEXT,
    ts        TEXT,
    path      TEXT NOT NULL,
    line      INTEGER NOT NULL,
    text      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS msgs_session_idx ON msgs(session);
CREATE INDEX IF NOT EXISTS msgs_ts_idx      ON msgs(ts);
CREATE INDEX IF NOT EXISTS msgs_path_idx    ON msgs(path);
CREATE VIRTUAL TABLE IF NOT EXISTS msgs_fts USING fts5(
    text,
    content='msgs',
    content_rowid='id',
    tokenize="unicode61 remove_diacritics 2"
);
"""

#: tables a `--full` rebuild is allowed to destroy. labels, excluded and meta
#: are user data and survive (AGENTS.md rule 4).
REBUILDABLE = ("msgs_fts", "msgs", "files")


def connect(path: Path | None = None) -> sqlite3.Connection:
    p = Path(path) if path is not None else config.db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(p)
    db.execute("PRAGMA foreign_keys=OFF")
    if db.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
        init_schema(db)
    return db


def init_schema(db: sqlite3.Connection) -> None:
    # WAL is persisted in the database header, so it only has to be set when the
    # schema is (re)created rather than on every connect.
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA)
    db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    db.commit()


def drop_content(db: sqlite3.Connection) -> None:
    """`index --full`: throw away derived data, keep user data."""
    db.executescript("".join(f"DROP TABLE IF EXISTS {t};" for t in REBUILDABLE))
    db.executescript(SCHEMA)
    db.commit()


# ------------------------------------------------------------------- meta k/v

def meta_get(db: sqlite3.Connection, k: str, default: str | None = None) -> str | None:
    row = db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return row[0] if row else default


def meta_set(db: sqlite3.Connection, k: str, v: str) -> None:
    db.execute(
        "INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
        (k, str(v)),
    )


# --------------------------------------------------- FTS5 external content

def delete_messages(db: sqlite3.Connection, where: str, params: tuple) -> int:
    """Delete rows from ``msgs`` honouring the FTS5 external-content protocol.

    With ``content=''`` FTS5 does not hold the text, so it cannot work out which
    postings to remove on its own: it has to be told, *with the original text*,
    before the content row disappears. Skipping this leaves stale postings and
    later queries return phantom rows or fail outright. Every delete in this
    codebase goes through here.
    """
    rows = db.execute(f"SELECT id, text FROM msgs WHERE {where}", params).fetchall()
    db.executemany(
        "INSERT INTO msgs_fts(msgs_fts, rowid, text) VALUES('delete', ?, ?)", rows
    )
    db.execute(f"DELETE FROM msgs WHERE {where}", params)
    return len(rows)


def fts_integrity_ok(db: sqlite3.Connection) -> bool:
    """True when the FTS index is consistent *with the content table*.

    Note the two forms. Plain ``VALUES('integrity-check')`` only verifies that
    the index is internally consistent - on SQLite 3.53 it happily passes after a
    raw ``DELETE FROM msgs``, leaving phantom rows behind. The ``rank=1`` form
    also compares the index against the content table, which is the check that
    actually matters for an external-content table. It is used where available
    and falls back on older SQLite.
    """
    try:
        db.execute("INSERT INTO msgs_fts(msgs_fts) VALUES('integrity-check')")
    except sqlite3.DatabaseError:
        return False
    try:
        db.execute("INSERT INTO msgs_fts(msgs_fts, rank) VALUES('integrity-check', 1)")
    except sqlite3.OperationalError:
        return True          # too old to support the argument
    except sqlite3.DatabaseError:
        return False
    return True
