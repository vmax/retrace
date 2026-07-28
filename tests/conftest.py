"""Fixtures: synthetic transcripts in both formats, and stub CLIs on PATH.

The whole suite runs with neither `claude` nor `codex` installed. Everything that
shells out is exercised against recording stub binaries, so argv, stdin, cwd,
exit codes and timeouts are all assertable.
"""

from __future__ import annotations

import itertools
import json
import os
import sqlite3
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from retrace import indexer, storage  # noqa: E402


# ------------------------------------------------------------------ environment

@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    """Isolate every RETRACE_* knob and both transcript roots."""
    for k in list(os.environ):
        if k.startswith("RETRACE_"):
            monkeypatch.delenv(k, raising=False)
    claude = tmp_path / "claude" / "projects"
    codex = tmp_path / "codex" / "sessions"
    claude.mkdir(parents=True)
    codex.mkdir(parents=True)
    monkeypatch.setenv("RETRACE_DB", str(tmp_path / "index.db"))
    monkeypatch.setenv("RETRACE_CLAUDE_ROOT", str(claude))
    monkeypatch.setenv("RETRACE_CODEX_ROOT", str(codex))
    monkeypatch.setenv("NO_COLOR", "1")
    return {"tmp": tmp_path, "claude": claude, "codex": codex,
            "db": tmp_path / "index.db"}


@pytest.fixture
def db(env):
    conn = storage.connect()
    yield conn
    conn.close()


# ------------------------------------------------------------------- transcripts

def jsonl(path: Path, objs, partial: str | None = None, append: bool = False) -> Path:
    """Write JSONL. ``partial`` appends a line with **no** trailing newline,
    which is what a live session looks like mid-write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a" if append else "w", encoding="utf-8") as f:
        for o in objs:
            f.write(json.dumps(o) + "\n")
        if partial is not None:
            f.write(partial)
    return path


_seq = itertools.count(1)


def claude_msg(role: str, text, ts="2026-06-01T10:00:00Z", session="s-1",
               cwd="/home/max/projects/infra", uuid=None, **extra):
    o = {
        "type": role,
        "sessionId": session,
        "timestamp": ts,
        "cwd": cwd,
        # unique per call: two entries sharing a uuid are the same entry as far
        # as the indexer is concerned, which is deliberate but not what a
        # fixture wants by accident
        "uuid": uuid or f"{session}-{role}-{next(_seq)}",
        "message": {"role": role,
                    "content": text if isinstance(text, (str, list)) else [text]},
    }
    o.update(extra)
    return o


def claude_rename(name: str, session="s-1", ts="2026-06-01T10:00:00Z",
                 cwd="/home/max/projects/infra"):
    """A `/rename` as Claude Code actually records it.

    A `type: "system"` entry with the command in `content` and no message body -
    so it carries no indexable text, which is exactly why the name has to be read
    off the raw line rather than out of the extracted text.
    """
    return {
        "type": "system", "subtype": "local_command_output", "level": "info",
        "sessionId": session, "timestamp": ts, "cwd": cwd,
        "uuid": f"{session}-rename-{next(_seq)}",
        "content": ("<command-name>/rename</command-name>\n"
                    "            <command-message>rename</command-message>\n"
                    f"            <command-args>{name}</command-args>"),
    }


def codex_meta(sid="codex-session-id", cwd="/home/max/projects/api", nested=True,
               ts="2026-06-02T09:00:00Z"):
    payload = {"type": "session_meta", "id": sid, "cwd": cwd}
    if nested:
        return {"timestamp": ts, "type": "session_meta", "payload": payload}
    # some versions put the kind marker only at the top level
    return {"timestamp": ts, "type": "session_meta", "id": sid, "cwd": cwd}


def codex_msg(role: str, text, ts="2026-06-02T09:01:00Z"):
    key = "input_text" if role == "user" else "output_text"
    return {
        "timestamp": ts,
        "type": "response_item",
        "payload": {"type": "message", "role": role,
                    "content": [{"type": key, "text": text}]},
    }


@pytest.fixture
def corpus(env):
    """A small but representative corpus in both formats."""
    cdir = env["claude"] / "-home-max-projects-infra"
    jsonl(cdir / "aaaaaaaa-1111-2222-3333-444444444444.jsonl", [
        claude_msg("user", "how do I debug mikrotik ipsec tunnels",
                   ts="2026-06-01T10:00:00Z", session="claude-sess-1"),
        claude_msg("assistant", "check the haproxy.cfg and conntrack table",
                   ts="2026-06-01T10:00:05Z", session="claude-sess-1"),
        claude_msg("user", "<command-name>/model</command-name>",
                   ts="2026-06-01T10:01:00Z", session="claude-sess-1"),
    ])
    jsonl(cdir / "bbbbbbbb-1111-2222-3333-444444444444.jsonl", [
        claude_msg("user", "error budget policy for the api gateway",
                   ts="2026-05-01T08:00:00Z", session="claude-sess-2",
                   cwd="/home/max/projects/gw"),
        claude_msg("assistant", [{"type": "thinking", "thinking": "considering slo math"},
                                 {"type": "text", "text": "burn rate alerts, two windows"}],
                   ts="2026-05-01T08:00:10Z", session="claude-sess-2",
                   cwd="/home/max/projects/gw"),
    ])
    ddir = env["codex"] / "2026" / "06" / "02"
    jsonl(ddir / "rollout-2026-06-02T09-00-00-cccccccc-1111-2222-3333-444444444444.jsonl", [
        codex_meta(sid="codex-real-id", cwd="/home/max/projects/api"),
        codex_msg("user", "rewrite the pagination cursor logic"),
        codex_msg("assistant", "use a keyset cursor over (created_at, id)"),
    ])
    return env


@pytest.fixture
def indexed(db, corpus):
    indexer.index(db, include_tools=False)
    return db


# ------------------------------------------------------------------ stub binaries

STUB = """#!{python}
import json, os, sys
rec = {{
    "argv": sys.argv,
    "stdin": sys.stdin.read() if not sys.stdin.isatty() else "",
    "cwd": os.getcwd(),
    "name": {name!r},
}}
with open({log!r}, "a") as f:
    f.write(json.dumps(rec) + "\\n")
{body}
"""


@pytest.fixture
def stubs(tmp_path, monkeypatch):
    """Install recording stubs on PATH. Returns a helper object."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")

    class Stubs:
        dir = bindir
        logfile = log

        def install(self, name: str, body: str = "sys.exit(0)") -> Path:
            p = bindir / name
            p.write_text(STUB.format(python=sys.executable, name=name,
                                     log=str(log), body=body))
            p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            return p

        def calls(self, name: str | None = None) -> list[dict]:
            if not log.exists():
                return []
            out = [json.loads(l) for l in log.read_text().splitlines() if l.strip()]
            return [c for c in out if name is None or c["name"] == name]

    return Stubs()


# ------------------------------------------------------------------- assertions

def fts_ok(conn: sqlite3.Connection) -> bool:
    return storage.fts_integrity_ok(conn)
