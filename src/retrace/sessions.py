"""Session addressing, labels, rendering, and deletion."""

from __future__ import annotations

import os
import re
import sqlite3

from . import storage
from .query import session_rows


class NotFound(Exception):
    pass


class Ambiguous(Exception):
    """More than one session matched. Never guessed at (AGENTS.md rule 3)."""

    def __init__(self, ref: str, rows: list[dict]):
        super().__init__(f"{len(rows)} sessions match {ref!r}")
        self.ref = ref
        self.rows = rows


def resolve_session(db: sqlite3.Connection, ref: str | None, limit: int = 40,
                    *, source: str | None = None, project: str | None = None) -> list[dict]:
    """Accept anything plausible: nothing (= most recent), an ordinal like ``2``
    (1 = newest), a session id or prefix, or a substring of path/project/label.

    ``source``/``project`` narrow the candidate set. They are not decoration: an
    ordinal counts into whatever list they describe, so `rm 1 --source codex` must
    delete the newest *codex* session and not the newest session overall.
    """
    ref = "" if ref is None else str(ref)
    filters = {"source": source, "project": project}
    if not ref:
        return session_rows(db, limit=1, **filters)
    if re.fullmatch(r"-?\d{1,3}", ref):
        idx = abs(int(ref))
        rows = session_rows(db, limit=max(idx, 1), **filters)
        return rows[idx - 1: idx] if 0 < idx <= len(rows) else []
    if db.execute("SELECT 1 FROM msgs WHERE session=? LIMIT 1", (ref,)).fetchone():
        # An exact id wins outright, and the lookup is unlimited: with `limit`
        # applied, an id that is also a substring of 40 newer sessions' paths
        # would fall off the end of the window and resolve to nothing.
        hit = session_by_id(db, ref, **filters)
        return [hit] if hit else []
    return session_rows(db, limit=limit, contains=ref, **filters)


def one_session(db: sqlite3.Connection, ref: str | None, **filters) -> dict:
    rows = resolve_session(db, ref, **filters)
    if not rows:
        raise NotFound(f"no session matching {ref!r}")
    if len(rows) > 1:
        raise Ambiguous(ref or "", rows)
    return rows[0]


def session_by_id(db: sqlite3.Connection, session: str, **filters) -> dict | None:
    for r in session_rows(db, limit=None, contains=session, **filters):
        if r["session"] == session:
            return r
    return None


def session_paths(db: sqlite3.Connection, session: str) -> list[str]:
    return [p for (p,) in db.execute(
        "SELECT DISTINCT path FROM msgs WHERE session=? ORDER BY path", (session,))]


# ---------------------------------------------------------------------- labels

def set_label(db: sqlite3.Connection, session: str, name: str) -> None:
    """Store a label in retrace's own database.

    Claude Code's real session names are only settable interactively or at
    start-up; the only other route is editing the single copy of the transcript,
    which the docs warn against, to change a display string. So this is ours -
    nothing on disk is touched and `claude --resume` keeps working unchanged.
    """
    db.execute(
        "INSERT INTO labels(session,name,ts) VALUES(?,?,datetime('now')) "
        "ON CONFLICT(session) DO UPDATE SET name=excluded.name, ts=excluded.ts",
        (session, name),
    )
    db.commit()


def clear_label(db: sqlite3.Connection, session: str) -> None:
    db.execute("DELETE FROM labels WHERE session=?", (session,))
    db.commit()


def get_label(db: sqlite3.Connection, session: str) -> str | None:
    row = db.execute("SELECT name FROM labels WHERE session=?", (session,)).fetchone()
    return row[0] if row else None


# -------------------------------------------------------------------- deletion

def delete_session(db: sqlite3.Connection, r: dict, purge: bool = True):
    """Drop a session from the index and, unless ``purge`` is False, unlink its
    transcripts. Returns ``(messages_dropped, files_removed, errors)``."""
    session = r["session"]
    paths = session_paths(db, session)
    n = storage.delete_messages(db, "session=?", (session,))
    for p in paths:
        db.execute(
            "INSERT OR REPLACE INTO excluded(path,session,ts) VALUES(?,?,datetime('now'))",
            (p, session),
        )
        db.execute("DELETE FROM files WHERE path=?", (p,))
    db.commit()

    gone, errors = 0, []
    if purge:
        removed = []
        for p in paths:
            try:
                os.unlink(p)
                gone += 1
                removed.append(p)
            except OSError as e:
                errors.append(f"{p}: {e}")
        # Drop the bookkeeping only for files that are actually gone: there is
        # nothing for those to come back to. A transcript we failed to unlink is
        # still on disk, so it stays excluded - otherwise the next indexing pass
        # would quietly pull the session the user just deleted back in.
        for p in removed:
            db.execute("DELETE FROM excluded WHERE path=?", (p,))
        if not errors:
            db.execute("DELETE FROM labels WHERE session=?", (session,))
        db.commit()
    return n, gone, errors


def restore(db: sqlite3.Connection, ref: str) -> list[str]:
    rows = db.execute(
        "SELECT path FROM excluded WHERE session LIKE ? OR path LIKE ?",
        (f"%{ref}%", f"%{ref}%"),
    ).fetchall()
    for (path,) in rows:
        db.execute("DELETE FROM excluded WHERE path=?", (path,))
    db.commit()
    return [p for (p,) in rows]


# --------------------------------------------------------------------- reading

def messages(db: sqlite3.Connection, session: str):
    return db.execute(
        "SELECT role,ts,line,text FROM msgs WHERE session=? ORDER BY path,line",
        (session,),
    ).fetchall()


def render_session(db: sqlite3.Connection, r: dict, mark_line: int | None = None,
                   plain: bool = False, palette=None):
    """Render a whole session as text.

    Returns ``(text, jump)`` where jump is the 1-based line of the marked
    message, so a pager can open at the right place. Two separate return values
    on purpose: packing them together once made every summary claim to be
    ``(clipped)``.

    ``plain=True`` emits no ANSI at all - what gets sent to a model
    (AGENTS.md rule 11).
    """
    bold, dim, off = ("", "", "") if plain else (palette or ("", "", ""))
    out = [
        f"{bold}{r['title']}{off}",
        f"{dim}{r['source']}  {r['session']}  {r['project'] or '-'}{off}",
        f"{dim}{(r['first'] or '')[:19]} .. {(r['last'] or '')[:19]}  "
        f"{r['n']} messages{off}",
    ]
    target = 1
    for role, ts, line, text in messages(db, r["session"]):
        if mark_line is not None and line == mark_line:
            target = len(out) + 2
        mark = ">>>" if mark_line is not None and line == mark_line else "---"
        out.append("")
        out.append(f"{bold}{mark} {role} {(ts or '')[:19]} {mark}{off}")
        out.append(text)
    return "\n".join(out) + "\n", target


def context_around(db: sqlite3.Connection, msg_id: int, before: int = 2, after: int = 6):
    """The rows around one message - the preview pane's content."""
    row = db.execute(
        "SELECT source,session,project,path,line FROM msgs WHERE id=?", (msg_id,)
    ).fetchone()
    if not row:
        return None, []
    src, sess, proj, path, line = row
    rows = db.execute(
        "SELECT role,ts,line,text FROM msgs WHERE path=? AND line BETWEEN ? AND ? "
        "ORDER BY line",
        (path, line - before, line + after),
    ).fetchall()
    head = {"source": src, "session": sess, "project": proj, "path": path, "line": line}
    return head, rows
