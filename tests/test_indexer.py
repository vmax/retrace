from __future__ import annotations

import json
import os
import pathlib

from retrace import indexer, storage
from conftest import claude_msg, codex_meta, codex_msg, jsonl


def test_incremental_skips_untouched_files(db, corpus, monkeypatch):
    indexer.index(db)
    opened = []
    real_open = type(next(iter(corpus["claude"].rglob("*.jsonl")))).open

    def spy(self, *a, **kw):
        opened.append(str(self))
        return real_open(self, *a, **kw)

    monkeypatch.setattr("pathlib.Path.open", spy)
    rep = indexer.index(db)
    assert rep.scanned == 3          # every file stat()ed
    assert rep.added == 0
    transcripts = [p for p in opened if p.endswith(".jsonl")
                   and "session_index" not in p]
    assert transcripts == [], "re-read a file that had not grown"


def test_incremental_reads_only_the_delta(db, env):
    p = env["claude"] / "-tmp-g" / "g.jsonl"
    jsonl(p, [claude_msg("user", "first", session="grow")])
    indexer.index(db)
    off_before = db.execute("SELECT off FROM files").fetchone()[0]

    jsonl(p, [claude_msg("user", "second", session="grow")], append=True)
    rep = indexer.index(db)
    assert rep.added == 1
    assert db.execute("SELECT off FROM files").fetchone()[0] > off_before
    assert [l for (l,) in db.execute("SELECT line FROM msgs ORDER BY line")] == [1, 2]


def test_truncated_file_restarts_from_zero(db, env):
    p = env["claude"] / "-tmp-t2" / "t.jsonl"
    jsonl(p, [claude_msg("user", "a" * 100, session="tr"),
              claude_msg("user", "b" * 100, session="tr")])
    indexer.index(db)
    jsonl(p, [claude_msg("user", "c", session="tr")])
    rep = indexer.index(db)
    assert rep.added == 1
    assert [t for (t,) in db.execute("SELECT text FROM msgs")] == ["c"]
    assert storage.fts_integrity_ok(db)


def test_missing_roots_are_reported_not_fatal(db, env, monkeypatch):
    monkeypatch.setenv("RETRACE_CODEX_ROOT", str(env["tmp"] / "nope"))
    rep = indexer.index(db)
    assert [s for s, _ in rep.missing_roots] == ["codex"]


def test_empty_lines_are_skipped_silently(db, env):
    p = env["claude"] / "-tmp-e" / "e.jsonl"
    p.parent.mkdir(parents=True)
    p.write_text("\n\n" + json.dumps(claude_msg("user", "after blanks",
                                                session="blank")) + "\n\n")
    rep = indexer.index(db)
    assert rep.skipped == []
    assert db.execute("SELECT count(*) FROM msgs").fetchone()[0] == 1


def test_text_is_capped(db, env, monkeypatch):
    monkeypatch.setattr("retrace.config.MAX_CHARS", 50)
    jsonl(env["claude"] / "-tmp-cap" / "c.jsonl",
          [claude_msg("user", "x" * 5000, session="cap")])
    indexer.index(db)
    assert len(db.execute("SELECT text FROM msgs").fetchone()[0]) == 50


def test_empty_text_is_not_indexed(db, env):
    jsonl(env["claude"] / "-tmp-blank" / "b.jsonl", [
        claude_msg("user", "   ", session="ws"),
        claude_msg("user", "real", session="ws"),
    ])
    indexer.index(db)
    assert [t for (t,) in db.execute("SELECT text FROM msgs")] == ["real"]


def test_codex_rows_written_before_session_meta_get_rekeyed(db, env):
    """The real id can appear after the first messages; rows written with the
    filename stem are corrected once it is known."""
    p = env["codex"] / "rollout-2026-06-09T09-00-00-late.jsonl"
    jsonl(p, [codex_msg("user", "before the meta line"),
              codex_meta(sid="late-real-id", cwd="/tmp/late")])
    indexer.index(db)
    assert {s for (s,) in db.execute("SELECT DISTINCT session FROM msgs")} == \
        {"late-real-id"}


def test_report_counts_are_sane(db, corpus):
    rep = indexer.index(db)
    assert rep.scanned == 3
    assert rep.added == 7            # 3 + 2 claude, 2 codex; meta lines excluded
    assert rep.skipped == []


def test_skipped_examples_are_bounded(db, env):
    p = env["claude"] / "-tmp-junk" / "j.jsonl"
    p.parent.mkdir(parents=True)
    p.write_text("{bad\n" * 2000)
    rep = indexer.index(db)
    assert len(rep.skipped) == indexer.SKIPPED_EXAMPLES   # examples, not the count


def test_same_size_rewrite_is_not_mistaken_for_untouched(db, env):
    """size alone is not a fingerprint: a transcript can be rewritten to exactly
    the same length, and calling that "untouched" leaves stale messages indexed."""
    p = env["claude"] / "-tmp-same" / "s.jsonl"
    jsonl(p, [claude_msg("user", "aaaa", session="same", uuid="u-1")])
    indexer.index(db)
    before = p.stat().st_size

    jsonl(p, [claude_msg("user", "bbbb", session="same", uuid="u-2")])
    os.utime(p, (p.stat().st_atime, p.stat().st_mtime + 5))
    assert p.stat().st_size == before, "the fixture is not testing what it claims"

    rep = indexer.index(db)
    assert rep.added == 1
    assert [t for (t,) in db.execute("SELECT text FROM msgs")] == ["bbbb"]
    assert storage.fts_integrity_ok(db)


def test_untouched_file_is_still_skipped(db, corpus):
    """The mtime check must not turn every pass into a full re-read."""
    indexer.index(db)
    rep = indexer.index(db)
    assert (rep.scanned, rep.added) == (3, 0)


def test_file_deleted_between_stat_and_open(db, env, monkeypatch):
    """A live corpus, and Claude Code prunes on its own schedule. One transcript
    disappearing mid-pass must not abort the freshness pass."""
    jsonl(env["claude"] / "-tmp-v1" / "gone.jsonl",
          [claude_msg("user", "about to vanish", session="v1")])
    jsonl(env["claude"] / "-tmp-v2" / "stays.jsonl",
          [claude_msg("user", "still here", session="v2")])

    real_open = pathlib.Path.open

    def vanishing(self, *a, **kw):
        if self.name == "gone.jsonl":
            raise FileNotFoundError(2, "No such file or directory", str(self))
        return real_open(self, *a, **kw)

    monkeypatch.setattr("pathlib.Path.open", vanishing)
    rep = indexer.index(db)
    assert rep.vanished == 1
    assert [t for (t,) in db.execute("SELECT text FROM msgs")] == ["still here"]


def test_a_vanished_file_leaves_nothing_behind(db, env):
    jsonl(env["claude"] / "-tmp-gone" / "g.jsonl",
          [claude_msg("user", "indexed then deleted", session="gone")])
    indexer.index(db)
    (env["claude"] / "-tmp-gone" / "g.jsonl").unlink()
    indexer.index(db)
    # the file is no longer discovered, so its rows stay until a --full rebuild;
    # what must not happen is a crash or a phantom in the FTS index
    assert storage.fts_integrity_ok(db)


def test_include_tools_change_rebuilds_existing_files(db, env):
    """Flipping the setting must apply to the whole corpus, not just new lines.

    An incremental pass only reads what grew, so without a rebuild the index
    would hold old messages parsed one way and new ones the other, with no way to
    tell which is which.
    """
    secret = "AKIAIOSFODNN7EXAMPLE"
    jsonl(env["claude"] / "-tmp-it" / "t.jsonl", [
        claude_msg("assistant", [
            {"type": "text", "text": "running it"},
            {"type": "tool_use", "name": "Bash", "input": {"command": f"echo {secret}"}},
        ], session="tools"),
    ])
    indexer.index(db, include_tools=False)
    assert secret not in " ".join(t for (t,) in db.execute("SELECT text FROM msgs"))

    rep = indexer.index(db, include_tools=True)      # no file changed on disk
    assert rep.rebuilt
    assert secret in " ".join(t for (t,) in db.execute("SELECT text FROM msgs"))
    assert storage.fts_integrity_ok(db)

    rep = indexer.index(db, include_tools=False)
    assert rep.rebuilt
    assert secret not in " ".join(t for (t,) in db.execute("SELECT text FROM msgs"))


def test_include_tools_rebuild_keeps_user_data(db, corpus):
    from retrace import sessions
    indexer.index(db, include_tools=False)
    sessions.set_label(db, "claude-sess-1", "survives a rebuild")
    indexer.index(db, include_tools=True)
    assert sessions.get_label(db, "claude-sess-1") == "survives a rebuild"


def test_unchanged_tools_setting_does_not_rebuild(db, corpus):
    indexer.index(db, include_tools=False)
    assert indexer.index(db, include_tools=False).rebuilt is False
    assert indexer.auto_index(db).rebuilt is False


def test_skipped_total_is_not_capped(db, env):
    """The example list is bounded; the count is not. "skipped 500" when 2000
    lines failed is a lie about the state of the index."""
    p = env["claude"] / "-tmp-junk2" / "j.jsonl"
    p.parent.mkdir(parents=True)
    p.write_text("{bad\n" * 2000)
    rep = indexer.index(db)
    assert rep.skipped_total == 2000
    assert len(rep.skipped) == indexer.SKIPPED_EXAMPLES
