from __future__ import annotations

import os

import pytest

from retrace import sessions
from conftest import claude_msg, jsonl


def test_empty_ref_is_the_most_recent(indexed):
    # the codex session is the newest in the fixture corpus
    assert sessions.resolve_session(indexed, "")[0]["session"] == "codex-real-id"


def test_ordinal_refs(indexed):
    assert sessions.one_session(indexed, "1")["session"] == "codex-real-id"
    assert sessions.one_session(indexed, "2")["session"] == "claude-sess-1"
    assert sessions.one_session(indexed, "3")["session"] == "claude-sess-2"
    assert sessions.resolve_session(indexed, "99") == []


def test_exact_session_id_is_unambiguous(indexed):
    """`claude-sess-1` is also a prefix of nothing else, but an exact id must win
    even when it is a substring of another session's id."""
    assert sessions.one_session(indexed, "claude-sess-1")["session"] == "claude-sess-1"


def test_substring_of_project(indexed):
    assert sessions.one_session(indexed, "projects/api")["session"] == "codex-real-id"


def test_substring_of_label(indexed):
    sessions.set_label(indexed, "claude-sess-2", "slo work")
    assert sessions.one_session(indexed, "slo work")["session"] == "claude-sess-2"


def test_ambiguous_raises_with_candidates(indexed):
    with pytest.raises(sessions.Ambiguous) as e:
        sessions.one_session(indexed, "claude-sess")
    assert len(e.value.rows) == 2


def test_not_found_raises(indexed):
    with pytest.raises(sessions.NotFound):
        sessions.one_session(indexed, "nothing-like-this")


def test_label_lifecycle(indexed):
    sessions.set_label(indexed, "claude-sess-1", "one")
    assert sessions.get_label(indexed, "claude-sess-1") == "one"
    sessions.set_label(indexed, "claude-sess-1", "two")
    assert sessions.get_label(indexed, "claude-sess-1") == "two"
    sessions.clear_label(indexed, "claude-sess-1")
    assert sessions.get_label(indexed, "claude-sess-1") is None


def test_render_session_marks_the_hit(indexed):
    r = sessions.session_by_id(indexed, "claude-sess-1")
    text, jump = sessions.render_session(indexed, r, mark_line=2)
    lines = text.splitlines()
    assert ">>>" in lines[jump - 1]
    assert "conntrack" in lines[jump]


def test_render_session_has_a_header(indexed):
    r = sessions.session_by_id(indexed, "codex-real-id")
    text, jump = sessions.render_session(indexed, r)
    assert jump == 1
    assert text.splitlines()[0] == r["title"]
    assert "codex-real-id" in text


def test_context_around(indexed):
    mid = indexed.execute(
        "SELECT id FROM msgs WHERE text LIKE '%conntrack%'").fetchone()[0]
    head, rows = sessions.context_around(indexed, mid)
    assert head["session"] == "claude-sess-1"
    assert any("conntrack" in r[3] for r in rows)
    assert len(rows) == 3


def test_context_around_missing_id(indexed):
    assert sessions.context_around(indexed, 999999) == (None, [])


def test_delete_index_only_keeps_the_file(indexed):
    r = sessions.session_by_id(indexed, "claude-sess-1")
    path = sessions.session_paths(indexed, r["session"])[0]
    n, gone, errors = sessions.delete_session(indexed, r, purge=False)
    assert (n, gone, errors) == (3, 0, [])
    import os
    assert os.path.exists(path)
    assert indexed.execute("SELECT count(*) FROM excluded").fetchone()[0] == 1


def test_purge_clears_label_and_exclusion(indexed):
    sessions.set_label(indexed, "claude-sess-1", "gone soon")
    r = sessions.session_by_id(indexed, "claude-sess-1")
    n, gone, errors = sessions.delete_session(indexed, r, purge=True)
    assert (n, gone, errors) == (3, 1, [])
    assert indexed.execute("SELECT count(*) FROM excluded").fetchone()[0] == 0
    assert sessions.get_label(indexed, "claude-sess-1") is None


def test_purge_reports_unlink_errors(indexed, monkeypatch):
    r = sessions.session_by_id(indexed, "claude-sess-1")
    monkeypatch.setattr("os.unlink", lambda p: (_ for _ in ()).throw(
        OSError("read-only fs")))
    n, gone, errors = sessions.delete_session(indexed, r, purge=True)
    assert gone == 0 and errors and "read-only fs" in errors[0]


def test_exact_id_survives_a_crowd_of_newer_substring_matches(db, env):
    """An exact id must resolve even when it is also a substring of dozens of
    newer sessions' paths - the substring window would otherwise hide it."""
    from retrace import indexer

    jsonl(env["claude"] / "-old" / "target.jsonl",
          [claude_msg("user", "the one I want", session="abc123",
                      ts="2020-01-01T00:00:00Z")])
    for i in range(60):
        jsonl(env["claude"] / "-noise" / f"abc123-{i}.jsonl",
              [claude_msg("user", f"noise {i}", session=f"noise-{i}",
                          ts=f"2026-06-{i % 28 + 1:02d}T00:00:00Z")])
    indexer.index(db)

    r = sessions.one_session(db, "abc123")
    assert r["session"] == "abc123"


def test_filters_narrow_an_ordinal(indexed):
    """`rm 1 --source codex` means the newest codex session, not the newest one."""
    assert sessions.one_session(indexed, "1")["session"] == "codex-real-id"
    assert sessions.one_session(indexed, "1", source="claude")["session"] == \
        "claude-sess-1"
    assert sessions.one_session(indexed, "2", source="claude")["session"] == \
        "claude-sess-2"


def test_filters_narrow_an_empty_ref(indexed):
    assert sessions.resolve_session(indexed, "", source="claude")[0]["session"] == \
        "claude-sess-1"


def test_filters_can_exclude_a_named_session(indexed):
    with pytest.raises(sessions.NotFound):
        sessions.one_session(indexed, "claude-sess-1", source="codex")


def test_filters_disambiguate(indexed):
    with pytest.raises(sessions.Ambiguous):
        sessions.one_session(indexed, "claude-sess")
    r = sessions.one_session(indexed, "claude-sess", project="gw")
    assert r["session"] == "claude-sess-2"


def test_failed_purge_keeps_the_file_excluded(indexed, monkeypatch):
    """A transcript we could not unlink is still on disk. If the exclusion is
    dropped anyway, the next indexing pass pulls the session the user just
    deleted straight back in."""
    from retrace import indexer

    r = sessions.session_by_id(indexed, "claude-sess-1")
    monkeypatch.setattr("os.unlink", lambda p: (_ for _ in ()).throw(
        OSError("read-only fs")))
    n, gone, errors = sessions.delete_session(indexed, r, purge=True)
    assert (gone, bool(errors)) == (0, True)
    assert indexed.execute("SELECT count(*) FROM excluded").fetchone()[0] == 1

    indexer.index(indexed)
    assert indexed.execute(
        "SELECT count(*) FROM msgs WHERE session='claude-sess-1'").fetchone()[0] == 0


def test_partial_purge_only_forgets_what_it_deleted(indexed, env, monkeypatch):
    """One session, two transcripts, one unlink fails: the survivor stays out of
    the index and the label is kept, because there is still something to restore."""
    from retrace import indexer

    # a second file for the same session
    jsonl(env["claude"] / "-home-max-projects-infra" / "part2.jsonl",
          [claude_msg("user", "continued elsewhere", session="claude-sess-1",
                      ts="2026-06-01T11:00:00Z")])
    indexer.index(indexed)
    sessions.set_label(indexed, "claude-sess-1", "keep while a file remains")
    r = sessions.session_by_id(indexed, "claude-sess-1")
    paths = sessions.session_paths(indexed, "claude-sess-1")
    assert len(paths) == 2

    real = os.unlink
    monkeypatch.setattr("os.unlink", lambda p: (_ for _ in ()).throw(
        OSError("busy")) if p.endswith("part2.jsonl") else real(p))

    n, gone, errors = sessions.delete_session(indexed, r, purge=True)
    assert (gone, len(errors)) == (1, 1)
    left = [p for (p,) in indexed.execute("SELECT path FROM excluded")]
    assert left == [p for p in paths if p.endswith("part2.jsonl")]
    assert sessions.get_label(indexed, "claude-sess-1") == "keep while a file remains"

    indexer.index(indexed)
    assert indexed.execute(
        "SELECT count(*) FROM msgs WHERE session='claude-sess-1'").fetchone()[0] == 0


def test_clean_purge_still_forgets_everything(indexed):
    sessions.set_label(indexed, "claude-sess-1", "gone")
    r = sessions.session_by_id(indexed, "claude-sess-1")
    sessions.delete_session(indexed, r, purge=True)
    assert indexed.execute("SELECT count(*) FROM excluded").fetchone()[0] == 0
    assert sessions.get_label(indexed, "claude-sess-1") is None
