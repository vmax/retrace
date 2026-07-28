from __future__ import annotations

import pytest

from retrace import query, sessions
from conftest import claude_msg, claude_rename, jsonl


def test_search_orders_newest_first(indexed):
    rows = query.search(indexed, "c")
    ts = [r["ts"] for r in rows]
    assert ts == sorted(ts, reverse=True)


def test_search_rank_uses_bm25(indexed):
    rows = query.search(indexed, "cursor", sort="rank")
    assert rows and all(r["score"] < 0 for r in rows)   # bm25 is negative in fts5


def test_search_since_until(indexed):
    assert query.search(indexed, "budget", since="2026-06-01") == []
    assert query.search(indexed, "budget", until="2026-06-01")


def test_search_bad_query_raises_badquery(indexed):
    with pytest.raises(query.BadQuery):
        query.search(indexed, 'NEAR("unclosed')


def test_search_no_limit(indexed):
    assert query.search(indexed, "c", limit=None)


def test_role_like_matches_sidechain(db, env):
    jsonl(env["claude"] / "-tmp-s" / "s.jsonl",
          [claude_msg("user", "sidechain question", session="sc", isSidechain=True)])
    from retrace import indexer
    indexer.index(db)
    assert db.execute("SELECT role FROM msgs").fetchone()[0] == "user/sub"
    assert query.search(db, "sidechain", role="user")


def test_session_rows_titles_skip_wrappers(indexed):
    r = sessions.session_by_id(indexed, "claude-sess-1")
    assert r["title"] == "how do I debug mikrotik ipsec tunnels"


def test_session_rows_title_prefers_the_label(indexed):
    sessions.set_label(indexed, "claude-sess-1", "my own name")
    r = sessions.session_by_id(indexed, "claude-sess-1")
    assert r["title"] == "my own name" and r["label"] == "my own name"


def test_clean_title_rejects_wrappers():
    for bad in ("<command-name>x", "Caveat: blah", "[tool:Bash] ls", "[result] out"):
        assert query.clean_title(bad) == ""
    assert query.clean_title("  keep   this ") == "keep this"


def test_titles_use_one_query(indexed):
    seen = []
    indexed.set_trace_callback(lambda s: seen.append(s))
    try:
        query.session_rows(indexed, limit=None)
    finally:
        indexed.set_trace_callback(None)
    top = [s for s in seen if not s.lstrip().startswith("--")]
    assert len(top) == 2, top          # the listing, plus one windowed title query


def test_titles_fall_back_when_there_is_no_user_text(db, env):
    jsonl(env["claude"] / "-tmp-nt" / "n.jsonl",
          [claude_msg("assistant", "only assistant text", session="notitle")])
    from retrace import indexer
    indexer.index(db)
    assert query.session_rows(db)[0]["title"] == "(no user text)"


def test_titles_above_the_bind_threshold(db, env, monkeypatch):
    monkeypatch.setattr(query, "_TITLE_BIND_MAX", 1)
    jsonl(env["claude"] / "-tmp-m" / "m1.jsonl",
          [claude_msg("user", "first session", session="m1")])
    jsonl(env["claude"] / "-tmp-m" / "m2.jsonl",
          [claude_msg("user", "second session", session="m2")])
    from retrace import indexer
    indexer.index(db)
    titles = {r["session"]: r["title"] for r in query.session_rows(db)}
    assert titles == {"m1": "first session", "m2": "second session"}


def test_session_rows_sort_old(indexed):
    new = [r["session"] for r in query.session_rows(indexed, sort="new")]
    old = [r["session"] for r in query.session_rows(indexed, sort="old")]
    assert new == list(reversed(old))


def test_project_rows_aggregate(indexed):
    rows = {r["project"]: r for r in query.project_rows(indexed)}
    assert rows["/home/max/projects/infra"]["sessions"] == 1
    assert rows["/home/max/projects/infra"]["messages"] == 3
    assert rows["/home/max/projects/api"]["sources"] == "codex"


def test_project_rows_source_filter(indexed):
    assert [r["project"] for r in query.project_rows(indexed, source="codex")] == \
        ["/home/max/projects/api"]


def test_message_feed_is_a_search(indexed):
    rows = query.message_feed(indexed, "curso")     # partial word
    assert rows and rows[0]["session"] == "codex-real-id"


def test_message_feed_empty_query_returns_nothing(indexed):
    # the browser lists sessions when the box is empty; that is session_rows'
    # job, not the feed's
    assert query.message_feed(indexed, "") == []


def test_excerpt_centres_on_the_match():
    text = "x" * 300 + " NEEDLE " + "y" * 300
    piece, at = query.excerpt(text, ["needle"], width=90)
    assert piece.startswith("…")
    assert piece[at:at + 6] == "NEEDLE"


def test_excerpt_without_a_match_shows_the_head():
    piece, at = query.excerpt("some text here", ["absent"], width=90)
    assert (piece, at) == ("some text here", -1)


def test_excerpt_flattens_whitespace():
    piece, _ = query.excerpt("a\n\n  b\tc", ["b"])
    assert piece == "a b c"


def test_query_terms_drops_operators_and_syntax():
    assert query.query_terms('foo AND "bar baz" NEAR(x) qux*') == \
        ["foo", "bar", "baz", "x", "qux"]


def test_match_count_is_unfiltered_and_cheap(indexed):
    seen = []
    indexed.set_trace_callback(lambda s: seen.append(s))
    try:
        n = query.match_count(indexed, "c")
    finally:
        indexed.set_trace_callback(None)
    assert n == len(query.message_feed(indexed, "c"))
    top = [s for s in seen if not s.lstrip().startswith("--")]
    assert len(top) == 1 and " join " not in top[0].lower()


def test_match_count_empty_query(indexed):
    assert query.match_count(indexed, "  ") == 0


def test_feed_rows_carry_addressing_fields(indexed):
    row = query.message_feed(indexed, "cursor")[0]
    assert set(row) == {"id", "source", "session", "project", "role", "ts",
                        "path", "line", "excerpt", "match_at"}


def test_sessions_named_finds_a_cli_name(db, env):
    """A session's name is not message text, so FTS5 cannot see it."""
    from retrace import indexer
    jsonl(env["claude"] / "-a" / "a.jsonl", [
        claude_msg("user", "nothing here says the name", session="n1"),
        claude_rename("data-fixes", session="n1"),
    ])
    indexer.index(db)
    assert query.message_feed(db, "data-fixes") == []
    named = query.sessions_named(db, "data-fixes")
    assert [r["session"] for r in named] == ["n1"]
    assert named[0]["title"] == "data-fixes"


def test_sessions_named_finds_a_label(indexed):
    sessions.set_label(indexed, "codex-real-id", "pagination rework")
    assert [r["session"] for r in query.sessions_named(indexed, "pagination rework")] \
        == ["codex-real-id"]


def test_sessions_named_finds_an_id_or_path(indexed):
    assert query.sessions_named(indexed, "codex-real")[0]["session"] == "codex-real-id"
    assert query.sessions_named(indexed, "rollout-2026-06-02")[0]["session"] == \
        "codex-real-id"


def test_sessions_named_honours_filters(indexed):
    assert query.sessions_named(indexed, "claude-sess", source="codex") == []
    assert len(query.sessions_named(indexed, "claude-sess", source="claude")) == 2


def test_sessions_named_empty_input(indexed):
    assert query.sessions_named(indexed, "   ") == []
