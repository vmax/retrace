"""Incremental indexing.

The whole design is "one stat() per transcript, read only what grew". A no-op
pass over a 60k-message corpus is tens of milliseconds, which is what lets
search/browse re-index before every query instead of asking the user to remember.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from . import config, storage
from .parsers import PARSERS, codex_names, prescan, rename_from_text


#: How many unparseable lines are kept as examples. The *count* is not capped -
#: a run that could not parse 40,000 lines has to be able to say 40,000.
SKIPPED_EXAMPLES = 500


class Report:
    """Counters from one indexing pass. Not a transcript type - safe to be one."""

    __slots__ = ("scanned", "added", "skipped", "skipped_total", "missing_roots",
                 "rebuilt", "vanished", "named")

    def __init__(self) -> None:
        self.scanned = 0
        self.added = 0
        self.skipped: list[str] = []       # examples, at most SKIPPED_EXAMPLES
        self.skipped_total = 0             # every unparseable line
        self.missing_roots: list[tuple[str, Path]] = []
        self.rebuilt = False               # the tools setting changed
        self.vanished = 0                  # files that disappeared mid-pass
        self.named = 0                     # sessions with a name set by the CLI

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<Report scanned={self.scanned} added={self.added} "
            f"skipped={self.skipped_total}>"
        )


def excluded_paths(db: sqlite3.Connection) -> set[str]:
    return {p for (p,) in db.execute("SELECT path FROM excluded")}


def index(db: sqlite3.Connection, include_tools: bool = False, roots=None) -> Report:
    """One incremental pass. Honours the exclusion list (AGENTS.md rule 5).

    Changing ``include_tools`` rebuilds instead: incremental passes only ever
    read what grew, so flipping the setting would otherwise apply to new messages
    while every already-indexed message kept whatever the old setting produced -
    a corpus half-indexed one way and half the other, with no way to tell which.
    """
    rep = Report()
    was = storage.meta_get(db, "include_tools")
    if was is not None and was != ("1" if include_tools else "0"):
        storage.drop_content(db)
        rep.rebuilt = True
    skip = excluded_paths(db)

    for source, root in roots if roots is not None else config.roots():
        if not root.is_dir():
            rep.missing_roots.append((source, root))
            continue
        for path in sorted(root.rglob("*.jsonl")):
            if str(path) in skip:
                continue
            rep.scanned += 1
            try:
                st = path.stat()
            except OSError:
                continue
            size, mtime = st.st_size, st.st_mtime

            row = db.execute(
                "SELECT size, off, mtime, project, sid, cli_name FROM files "
                "WHERE path=?", (str(path),),
            ).fetchone()

            off = 0
            meta = None
            if row:
                if row[0] == size and row[2] == mtime:
                    continue                     # untouched since the last run
                if size < row[0] or row[2] > mtime or row[0] == size:
                    # Shrunk, rewritten in place, or rewritten to exactly the same
                    # length. Any of those means the stored offset is meaningless,
                    # so start over - and drop what we had for this file first, or
                    # the rebuild would sit on top of stale rows.
                    storage.delete_messages(db, "path=?", (str(path),))
                else:
                    off = row[1]
                    # carry the name forward: an append that contains no /rename
                    # must not erase the one an earlier pass found
                    meta = {"project": row[3], "sid": row[4], "cli_name": row[5]}
            if meta is None or not meta.get("project"):
                scanned = prescan(source, path)
                if meta is not None:
                    scanned["cli_name"] = meta.get("cli_name") or scanned["cli_name"]
                meta = scanned

            try:
                rep.added += ingest(
                    db, source, path, off, meta, include_tools, rep
                )
            except OSError:
                # Deleted or rotated between the stat() above and the open()
                # below - a live corpus, and Claude Code prunes on its own
                # schedule. One vanished transcript must not end the pass.
                rep.vanished += 1
                db.execute("DELETE FROM files WHERE path=?", (str(path),))
                storage.delete_messages(db, "path=?", (str(path),))
        db.commit()

    rep.named = sync_codex_names(db)
    storage.meta_set(db, "include_tools", "1" if include_tools else "0")
    db.commit()
    return rep


def sync_codex_names(db: sqlite3.Connection) -> int:
    """Copy Codex's own session names into `files.cli_name`.

    Codex does not record a rename in the transcript - it keeps names in a
    sibling `session_index.jsonl`, keyed by the same id `codex resume` takes. So
    unlike Claude Code, where the rename is a command entry in the conversation,
    this cannot be found by reading transcripts at all.
    """
    path = config.codex_index_path()
    try:
        stamp = f"{path.stat().st_size}:{path.stat().st_mtime}"
    except OSError:
        return 0
    if storage.meta_get(db, "codex_index_stamp") == stamp:
        return 0                      # unchanged, and this runs before queries
    storage.meta_set(db, "codex_index_stamp", stamp)

    names = codex_names(path)
    if not names:
        return 0
    rows = db.execute(
        "SELECT path, sid, cli_name FROM files WHERE sid IS NOT NULL").fetchall()
    changed = 0
    for path, sid, current in rows:
        want = names.get(sid)
        if want and want != current:
            db.execute("UPDATE files SET cli_name=? WHERE path=?", (want, path))
            changed += 1
    return changed


def full_reindex(db: sqlite3.Connection, include_tools: bool = False, roots=None) -> Report:
    """Drop and rebuild the derived tables. labels/excluded/meta survive."""
    storage.drop_content(db)
    return index(db, include_tools, roots)


def ingest(
    db: sqlite3.Connection,
    source: str,
    path: Path,
    off: int,
    meta: dict,
    include_tools: bool,
    rep: Report | None = None,
) -> int:
    parser = PARSERS[source]
    # Newer Claude Code writes subagent turns to `<session>/subagents/*.jsonl`
    # instead of flagging them with isSidechain in the main transcript. Same
    # meaning, so the same `/sub` role suffix, which keeps the role filter honest.
    sidechain = "subagents" in path.parts
    cli_name = meta.get("cli_name")
    fallback_session = meta.get("sid") or path.stem
    sid = meta.get("sid")
    project = meta.get("project") or ""

    with path.open("rb") as f:
        f.seek(off)
        blob = f.read()
        mtime = os.fstat(f.fileno()).st_mtime   # of the bytes we actually read

    chunks = blob.split(b"\n")
    # A final chunk with no trailing newline is a half-written line from a live
    # session. Do not parse it, and do not advance the stored offset past it:
    # it gets picked up whole on the next pass (AGENTS.md rule 6).
    tail = chunks.pop()
    consumed = len(blob) - len(tail)

    lineno = db.execute(
        "SELECT coalesce(max(line),0) FROM msgs WHERE path=?", (str(path),)
    ).fetchone()[0]

    added = 0
    for raw in chunks:
        lineno += 1
        if not raw.strip():
            continue
        line = raw.decode("utf-8", "replace")

        # Straight off the raw line, before parsing: Claude Code records the
        # /rename command in a `type: "system"` entry, which has no message body
        # and is deliberately not indexed - so looking for the rename in the
        # extracted text finds only the minority of cases that happen to be user
        # entries. The marker is unambiguous enough to read where it lies.
        if "/rename" in line:
            renamed = rename_from_text(line)
            if renamed:
                cli_name = renamed        # last rename in the file wins

        try:
            obj = json.loads(line)
        except ValueError:
            _note(rep, path, lineno, "invalid JSON")
            continue
        if not isinstance(obj, dict):
            continue
        try:
            got = parser(obj, fallback_session, include_tools)
        except Exception as e:            # a shape we have never seen
            # One bad line must never abort the run: it is counted, not raised.
            _note(rep, path, lineno, f"{type(e).__name__}: {e}")
            continue
        if not got:
            continue

        if got["cwd"]:
            project = got["cwd"]          # a literal cwd always beats the slug
        session = got["session"] or fallback_session
        if got["role"] == "meta":
            if session and session != path.stem:
                sid = session             # authoritative codex id
            continue
        text = (got["text"] or "").strip()
        if not text:
            continue

        role = got["role"]
        if sidechain and not role.endswith("/sub"):
            role = f"{role}/sub"
        text = text[: config.MAX_CHARS]
        key = got.get("key")
        entry_key = f"{path}\x00{key if key else lineno}"
        cur = db.execute(
            "INSERT INTO msgs(entry_key,source,session,project,role,ts,path,line,text) "
            "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(entry_key) DO NOTHING",
            (entry_key, source, sid or session, project, role, got["ts"],
             str(path), lineno, text),
        )
        if cur.rowcount:                  # 0 = already ingested, so no FTS row
            db.execute(
                "INSERT INTO msgs_fts(rowid,text) VALUES(?,?)", (cur.lastrowid, text)
            )
            added += 1

    db.execute(
        "INSERT INTO files(path,size,off,mtime,project,sid,cli_name) "
        "VALUES(?,?,?,?,?,?,?) "
        "ON CONFLICT(path) DO UPDATE SET size=excluded.size, off=excluded.off, "
        "mtime=excluded.mtime, project=excluded.project, sid=excluded.sid, "
        "cli_name=excluded.cli_name",
        (str(path), off + len(blob), off + consumed, mtime, project, sid, cli_name),
    )
    # Sessions are keyed by the codex id, which is only known once session_meta
    # has been read - rows written before that used the filename stem.
    if sid and sid != path.stem:
        db.execute(
            "UPDATE msgs SET session=? WHERE path=? AND session<>?",
            (sid, str(path), sid),
        )
    return added


def _note(rep: Report | None, path: Path, lineno: int, why: str) -> None:
    if rep is None:
        return
    rep.skipped_total += 1
    if len(rep.skipped) < SKIPPED_EXAMPLES:
        rep.skipped.append(f"{path}:{lineno}: {why}")


def auto_index(db: sqlite3.Connection) -> Report | None:
    """Freshness pass before a query. ``RETRACE_NO_AUTO=1`` disables it.

    The ``--include-tools`` choice is read back from meta so an automatic pass
    never silently changes what is indexed.
    """
    if config.no_auto_index():
        return None
    return index(db, storage.meta_get(db, "include_tools", "0") == "1")
