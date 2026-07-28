"""The rules in AGENTS.md, one test each.

Breaking any of them causes data loss or silent corruption.
"""

from __future__ import annotations

import hashlib
import os

import pytest

from retrace import cli, indexer, parsers, sessions, storage
from conftest import claude_msg, codex_meta, codex_msg, jsonl


def digest(root):
    return {p: hashlib.md5(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*.jsonl"))}


# 1 - never write to a transcript file
def test_i01_transcripts_are_never_written(db, corpus, env):
    before = digest(env["tmp"])
    assert before
    indexer.index(db)
    indexer.index(db)                                   # second, no-op pass
    from retrace import query
    query.search(db, "ipsec")
    r = sessions.session_by_id(db, "claude-sess-1")
    sessions.render_session(db, r)
    sessions.set_label(db, "claude-sess-1", "labelled")
    assert digest(env["tmp"]) == before


def test_i01_transcripts_are_opened_read_only(corpus, monkeypatch, db):
    """No code path may open a transcript for anything but reading."""
    real = type(next(iter(corpus["claude"].rglob("*.jsonl")))).open
    seen = []

    def spy(self, mode="r", *a, **kw):
        seen.append(mode)
        return real(self, mode, *a, **kw)

    monkeypatch.setattr("pathlib.Path.open", spy)
    indexer.full_reindex(db)
    assert seen and all(m.startswith("r") for m in seen), seen


# 2 - FTS5 external-content deletion protocol
def test_i02_delete_keeps_the_fts_index_consistent(indexed):
    r = sessions.session_by_id(indexed, "claude-sess-1")
    before = len(indexed.execute(
        "SELECT * FROM msgs_fts WHERE msgs_fts MATCH '\"ipsec\"'").fetchall())
    assert before == 1

    sessions.delete_session(indexed, r, purge=False)

    after = indexed.execute(
        "SELECT count(*) FROM msgs_fts WHERE msgs_fts MATCH '\"ipsec\"'").fetchone()[0]
    assert after == 0, "phantom row: postings outlived the content row"
    # the documented check, plus the stronger index-vs-content form
    indexed.execute("INSERT INTO msgs_fts(msgs_fts) VALUES('integrity-check')")
    assert storage.fts_integrity_ok(indexed)


def test_i02_naive_delete_is_detectably_broken(indexed):
    """Confirms the check above is meaningful.

    Skip the protocol and the index keeps its postings: the row still matches
    even though its content is gone. Note that bare
    ``VALUES('integrity-check')`` does *not* notice - on SQLite 3.53 it only
    checks the index against itself, which is why storage.fts_integrity_ok also
    runs the rank=1 form that compares index to content.
    """
    indexed.execute("DELETE FROM msgs WHERE session='claude-sess-1'")
    phantom = indexed.execute(
        "SELECT count(*) FROM msgs_fts WHERE msgs_fts MATCH '\"ipsec\"'").fetchone()[0]
    assert phantom == 1, "expected a stale posting to prove the point"
    assert storage.fts_integrity_ok(indexed) is False


def test_i02_every_delete_path_goes_through_storage(indexed, env):
    import inspect
    for mod in (sessions, indexer, cli):
        src = inspect.getsource(mod)
        for line in src.splitlines():
            s = line.strip()
            if s.startswith("#"):
                continue
            assert "DELETE FROM msgs " not in s and 'DELETE FROM msgs"' not in s, s


def test_i02_reindex_after_delete_is_clean(indexed, env):
    r = sessions.session_by_id(indexed, "codex-real-id")
    sessions.delete_session(indexed, r, purge=True)
    indexer.index(indexed)
    assert storage.fts_integrity_ok(indexed)
    indexer.full_reindex(indexed)
    assert storage.fts_integrity_ok(indexed)


# 3 - destructive actions confirm, and fail closed
def test_i03_rm_without_a_tty_aborts(indexed, monkeypatch, capsys, env):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *_: "")   # empty = no
    path = sessions.session_paths(indexed, "claude-sess-1")[0]
    with pytest.raises(SystemExit) as e:
        cli.main(["rm", "claude-sess-1"])
    assert e.value.code == 1
    assert os.path.exists(path)


def test_i03_no_tty_never_proceeds(indexed, monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    path = sessions.session_paths(indexed, "claude-sess-1")[0]
    with pytest.raises(SystemExit) as e:
        cli.main(["rm", "claude-sess-1"])
    assert e.value.code == 1
    assert os.path.exists(path), "deleted a file with nothing to confirm on"


def test_i03_yes_is_the_only_bypass(indexed, monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    path = sessions.session_paths(indexed, "claude-sess-1")[0]
    cli.main(["rm", "claude-sess-1", "-y"])
    assert not os.path.exists(path)


def test_i03_ambiguous_ref_lists_candidates_and_exits_1(indexed, capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["show", "claude-sess"])            # matches two
    assert e.value.code == 1
    err = capsys.readouterr().err
    assert "2 sessions match" in err
    assert "claude-sess-1" in err and "claude-sess-2" in err


# 4 - labels, excluded and meta survive `index --full`
def test_i04_user_data_survives_a_full_rebuild(indexed, env):
    sessions.set_label(indexed, "claude-sess-1", "keep me")
    indexed.execute("INSERT INTO excluded(path,session,ts) VALUES('/x','s',null)")
    storage.meta_set(indexed, "include_tools", "1")
    indexed.commit()

    indexer.full_reindex(indexed, include_tools=True)

    assert sessions.get_label(indexed, "claude-sess-1") == "keep me"
    assert indexed.execute("SELECT count(*) FROM excluded WHERE path='/x'"
                           ).fetchone()[0] == 1
    assert storage.meta_get(indexed, "include_tools") == "1"


# 5 - exclusions honoured by both incremental and full indexing
@pytest.mark.parametrize("run", [indexer.index, indexer.full_reindex])
def test_i05_excluded_files_stay_out(indexed, run):
    r = sessions.session_by_id(indexed, "claude-sess-1")
    sessions.delete_session(indexed, r, purge=False)     # index-only removal
    assert indexed.execute("SELECT count(*) FROM excluded").fetchone()[0] >= 1

    run(indexed, include_tools=False)
    assert indexed.execute(
        "SELECT count(*) FROM msgs WHERE session='claude-sess-1'").fetchone()[0] == 0


def test_i05_restore_brings_it_back(indexed):
    r = sessions.session_by_id(indexed, "claude-sess-1")
    sessions.delete_session(indexed, r, purge=False)
    sessions.restore(indexed, "claude-sess-1")
    indexer.index(indexed)
    assert indexed.execute(
        "SELECT count(*) FROM msgs WHERE session='claude-sess-1'").fetchone()[0] == 3


# 6 - partial trailing lines are not consumed
def test_i06_partial_trailing_line_is_left_alone(db, env):
    p = env["claude"] / "-tmp-live" / "live.jsonl"
    good = claude_msg("user", "complete line", session="live")
    import json as _json
    jsonl(p, [good], partial=_json.dumps(
        claude_msg("assistant", "half written", session="live"))[:40])

    indexer.index(db)
    assert [t for (t,) in db.execute("SELECT text FROM msgs")] == ["complete line"]
    size, off = db.execute("SELECT size, off FROM files").fetchone()
    assert off < size, "offset advanced past a partial line"

    # the session finishes writing that line
    with p.open("a") as f:
        f.write(_json.dumps(claude_msg("assistant", "half written now whole",
                                       session="live"))[40:] + "\n")
    indexer.index(db)
    texts = [t for (t,) in db.execute("SELECT text FROM msgs ORDER BY line")]
    assert texts == ["complete line", "half written now whole"]
    assert not any("half written" == t for t in texts[:1])


# 7 - stable row key, so re-ingesting is idempotent
def test_i07_reingest_is_idempotent(indexed, env):
    n = indexed.execute("SELECT count(*) FROM msgs").fetchone()[0]
    # force a re-read of every file from offset 0 without dropping rows
    indexed.execute("UPDATE files SET off=0, size=size+1")
    indexed.commit()
    indexer.index(indexed)
    assert indexed.execute("SELECT count(*) FROM msgs").fetchone()[0] == n
    assert storage.fts_integrity_ok(indexed)


def test_i07_entry_key_prefers_the_transcript_uuid(db, env):
    jsonl(env["claude"] / "-tmp-k" / "k.jsonl",
          [claude_msg("user", "keyed", session="k", uuid="uuid-abc")])
    indexer.index(db)
    key = db.execute("SELECT entry_key FROM msgs").fetchone()[0]
    assert key.endswith("uuid-abc")


def test_i07_rewritten_file_does_not_duplicate(db, env):
    p = env["claude"] / "-tmp-r" / "r.jsonl"
    jsonl(p, [claude_msg("user", "one", session="rw"),
              claude_msg("user", "two", session="rw")])
    indexer.index(db)
    jsonl(p, [claude_msg("user", "one", session="rw")])      # truncated rewrite
    indexer.index(db)
    assert [t for (t,) in db.execute("SELECT text FROM msgs")] == ["one"]
    assert storage.fts_integrity_ok(db)


# 8 - only final assistant/user text is indexed by default
def test_i08_tool_output_is_excluded_by_default(db, env):
    secret = "AKIAIOSFODNN7EXAMPLE"
    jsonl(env["claude"] / "-tmp-t" / "t.jsonl", [
        claude_msg("assistant", [
            {"type": "text", "text": "running it now"},
            {"type": "tool_use", "name": "Bash", "input": {"command": f"echo {secret}"}},
        ], session="tools"),
        claude_msg("user", [{"type": "tool_result", "content": secret}], session="tools"),
    ])
    indexer.index(db)
    allofit = " ".join(t for (t,) in db.execute("SELECT text FROM msgs"))
    assert secret not in allofit
    assert "running it now" in allofit

    indexer.full_reindex(db, include_tools=True)          # opt in explicitly
    allofit = " ".join(t for (t,) in db.execute("SELECT text FROM msgs"))
    assert secret in allofit


def test_i08_auto_index_does_not_change_the_tools_setting(indexed, env):
    storage.meta_set(indexed, "include_tools", "0")
    indexed.commit()
    indexer.auto_index(indexed)
    assert storage.meta_get(indexed, "include_tools") == "0"


def test_i08_codex_instructions_are_never_indexed(db, env):
    p = (env["codex"] / "2026" / "06" / "05" /
         "rollout-2026-06-05T09-00-00-eeeeeeee-1111-2222-3333-444444444444.jsonl")
    boiler = "You are Codex, a coding agent. " * 50
    jsonl(p, [
        {"timestamp": "2026-06-05T09:00:00Z", "type": "session_meta",
         "payload": {"type": "session_meta", "id": "instr", "cwd": "/tmp/i",
                     "instructions": boiler}},
        codex_msg("user", "actual question"),
    ])
    indexer.index(db)
    allofit = " ".join(t for (t,) in db.execute("SELECT text FROM msgs"))
    assert "You are Codex" not in allofit
    assert "actual question" in allofit


# 9 - never send ANSI escapes to a model
def test_i09_plain_render_has_no_escapes(indexed):
    r = sessions.session_by_id(indexed, "claude-sess-1")
    text, _ = sessions.render_session(indexed, r, plain=True,
                                      palette=("\033[1m", "\033[2m", "\033[0m"))
    assert "\033" not in text


def test_i09_summarize_sends_plain_text(indexed, stubs, monkeypatch):
    stubs.install("claude", "sys.stdout.write('a summary')")
    from retrace import actions
    r = sessions.session_by_id(indexed, "claude-sess-1")
    assert actions.summarize_session(indexed, r) == "a summary"
    sent = stubs.calls("claude")[0]["stdin"]
    assert sent and "\033" not in sent
