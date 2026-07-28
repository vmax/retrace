"""Bugs that actually happened, one test each.

Numbering follows the table at the end of AGENTS.md, so a failure names the bug.
"""

from __future__ import annotations

import hashlib

import pytest

from retrace import actions, indexer, parsers, query, sessions, storage
from conftest import claude_msg, codex_meta, codex_msg, jsonl


# 1 - TypeError: unhashable type: 'dict' on index
def test_r01_dict_valued_type_does_not_crash(db, env):
    """`node.get("type")` returned a JSON Schema object: `{"type": {"type": ...}}`.
    A dict is unhashable, so set membership raised."""
    hostile = {
        "type": "assistant",
        "sessionId": "schema-sess",
        "timestamp": "2026-06-01T00:00:00Z",
        "cwd": "/tmp/x",
        "message": {"role": "assistant", "content": [
            {"type": {"type": "string"}, "text": "still indexable"},
            {"type": "tool_use", "name": "Bash", "input_schema": {
                "type": "object",
                "properties": {"cmd": {"type": {"type": "string"}}},
            }},
        ]},
    }
    jsonl(env["claude"] / "-tmp-x" / "schema.jsonl", [hostile])
    rep = indexer.index(db)
    assert rep.skipped == []
    assert db.execute(
        "SELECT count(*) FROM msgs WHERE text LIKE '%still indexable%'"
    ).fetchone()[0] == 1


def test_r01_kind_helper_coerces_non_string_type():
    assert parsers._kind({"type": {"type": "string"}}) is None
    assert parsers._kind({"type": ["a", "b"]}) is None
    assert parsers._kind({"type": "text"}) == "text"


def test_r01_schema_keys_are_not_descended():
    out: list[str] = []
    parsers.harvest({"input_schema": {"description": "leaked"},
                     "instructions": {"text": "system prompt"},
                     "text": "kept"}, out)
    assert out == ["kept"]


# 2 - crash on non-string role/timestamp
@pytest.mark.parametrize("role", [{"name": "user"}, ["user"], 7, None])
@pytest.mark.parametrize("ts", [{"iso": "2026-01-01"}, ["2026"], 12345])
def test_r02_non_string_role_and_timestamp(db, env, role, ts):
    obj = {"type": "user", "sessionId": "wobbly", "timestamp": ts, "cwd": "/tmp/w",
           "message": {"role": role, "content": "text survives anyway"}}
    jsonl(env["claude"] / "-tmp-w" / "w.jsonl", [obj])
    rep = indexer.index(db)
    assert rep.skipped == []
    got = db.execute("SELECT role, ts FROM msgs").fetchone()
    assert isinstance(got[0], str)
    assert got[1] is None or isinstance(got[1], str)


def test_r02_as_text_never_returns_a_container():
    for v in ({"a": 1}, ["a"], 3, 3.5, True, None, "s"):
        out = parsers.as_text(v)
        assert out is None or isinstance(out, str)


# 3 - one bad line aborted the whole index run
def test_r03_bad_line_is_isolated(db, env):
    p = env["claude"] / "-tmp-b" / "b.jsonl"
    jsonl(p, [claude_msg("user", "first good line", session="iso")])
    with p.open("a") as f:
        f.write("{not json at all\n")
        f.write("[1,2,3]\n")                       # valid JSON, wrong type
    jsonl(p, [claude_msg("user", "line after the bad ones", session="iso")],
          append=True)

    rep = indexer.index(db)
    texts = [t for (t,) in db.execute("SELECT text FROM msgs ORDER BY line")]
    assert texts == ["first good line", "line after the bad ones"]
    assert len(rep.skipped) == 1                   # counted, not raised


def test_r03_parser_exception_is_counted_not_raised(db, env, monkeypatch):
    def boom(obj, fallback, include_tools=False):
        raise RuntimeError("unknown shape")

    monkeypatch.setitem(parsers.PARSERS, "claude", boom)
    jsonl(env["claude"] / "-tmp-c" / "c.jsonl", [claude_msg("user", "x")])
    rep = indexer.index(db)
    assert len(rep.skipped) == 1
    assert "RuntimeError" in rep.skipped[0]


# 4 - picker returned 0 hits for `mik` while `s mikrotik` worked
def test_r04_prefix_mode_matches_partial_words(indexed):
    assert query.search(indexed, "mik", exact=True) == []
    hits = query.search(indexed, "mik")
    assert hits and "mikrotik" in hits[0]["snippet"].lower()


def test_r04_prep_query_shapes():
    assert query.prep_query("mik", prefix=True) == '"mik"*'
    assert query.prep_query("mik") == '"mik"'
    assert query.prep_query("haproxy.cfg") == '"haproxy.cfg"'
    assert query.prep_query("a AND b") == "a AND b"     # operators pass through
    assert query.prep_query('"phrase here"') == '"phrase here"'


def test_r04_punctuation_would_be_a_syntax_error_unquoted(indexed):
    # the point of quoting: this is a syntax error in FTS5, not a zero-result query
    with pytest.raises(Exception):
        indexed.execute("SELECT * FROM msgs_fts WHERE msgs_fts MATCH 'haproxy.cfg'"
                        ).fetchall()
    assert query.search(indexed, "haproxy.cfg")


# 5 - cancelling an action was reported as a failure
def test_r05_cancellation_is_not_an_error(indexed, monkeypatch):
    """A user cancelling an action is not an error: exit 0, or 130 for an
    interrupt, but never a failure code that a script would trip over."""
    from retrace import cli

    monkeypatch.setattr("sys.argv", ["retrace"])
    monkeypatch.setattr(cli, "cmd_browse", lambda args: (_ for _ in ()).throw(
        KeyboardInterrupt()))
    assert cli.main(["stats"]) == 0
    assert cli.main(["browse"]) == 130          # 130 = cancelled, distinct from 1


# 6 - codex sessions keyed by filename, so resume broke
@pytest.mark.parametrize("nested", [True, False])
def test_r06_codex_kind_marker_at_either_level(db, env, nested):
    p = (env["codex"] / "2026" / "07" / "01" /
         "rollout-2026-07-01T09-00-00-dddddddd-1111-2222-3333-444444444444.jsonl")
    jsonl(p, [codex_meta(sid="the-real-id", nested=nested),
              codex_msg("user", "hello codex")])
    indexer.index(db)
    assert {s for (s,) in db.execute("SELECT DISTINCT session FROM msgs")} == {"the-real-id"}
    assert db.execute("SELECT sid FROM files").fetchone()[0] == "the-real-id"


def test_r06_payload_wins_when_both_present():
    obj = {"type": "response_item", "payload": {"type": "session_meta", "id": "inner"}}
    got = parsers.parse_codex(obj, "fallback")
    assert got["role"] == "meta" and got["session"] == "inner"


def test_r06_resume_uses_the_codex_id_not_the_filename(indexed):
    r = sessions.session_by_id(indexed, "codex-real-id")
    assert r is not None
    assert actions.resume_argv(r) == ["codex", "resume", "codex-real-id"]


# 7 - keystroke latency 65ms -> 1.28s: a count(*) joined msgs to apply a filter
def test_r07_feed_does_not_run_a_counting_join(indexed):
    seen: list[str] = []
    indexed.set_trace_callback(lambda sql: seen.append(" ".join(sql.split())))
    try:
        query.message_feed(indexed, "cursor")
    finally:
        indexed.set_trace_callback(None)
    assert seen, "trace callback saw nothing - the test is not watching anything"
    joins = [s for s in seen if "count(" in s.lower() and " join " in s.lower()]
    assert joins == [], joins


def test_r07_one_statement_per_keystroke(indexed):
    """A keystroke is one query. Anything else is per-query setup creeping back
    in - connect() must not run schema or migration work either."""
    seen: list[str] = []
    indexed.set_trace_callback(lambda sql: seen.append(sql))
    try:
        query.message_feed(indexed, "cursor")
    finally:
        indexed.set_trace_callback(None)
    # FTS5 issues its own internal statements, which sqlite prefixes with "--"
    top = [s for s in seen if not s.lstrip().startswith("--")]
    assert len(top) == 1, top


def test_r07_connect_does_no_setup_work(env, monkeypatch):
    """connect() on an existing index must not touch the schema.

    Running executescript(SCHEMA) plus a PRAGMA table_info migration probe on
    every call is free behind 60ms of interpreter start-up and expensive without
    it. Sabotage the setup path and require connect() not to reach it.
    """
    from retrace import storage as st

    st.connect().close()                                  # creates the schema
    monkeypatch.setattr(st, "SCHEMA", "SELECT deliberately not valid sql((")
    monkeypatch.setattr(st, "init_schema", lambda db: pytest.fail(
        "connect() re-ran schema setup on an existing index"))
    db = st.connect()
    assert db.execute("SELECT count(*) FROM msgs").fetchone()[0] == 0
    db.close()


# 8 - every summary claimed to be `(clipped)`: two values packed into one
def test_r08_clip_flag_is_separate_from_the_jump_line(indexed):
    text, clipped = actions.clamp_transcript("short", limit=1000)
    assert (text, clipped) == ("short", False)
    text, clipped = actions.clamp_transcript("x" * 5000, limit=1000)
    assert clipped and "elided from the middle" in text

    r = sessions.session_by_id(indexed, "claude-sess-1")
    rendered, jump = sessions.render_session(indexed, r, mark_line=2)
    assert isinstance(jump, int) and jump > 1        # a line number, not a bool


# 9 - `codex exec` exited 1 without --skip-git-repo-check
def test_r09_codex_summarize_flags_are_present():
    argv = actions.summarize_argv("codex", prompt="P")
    assert argv[:2] == ["codex", "exec"]
    for flag in ("--ephemeral", "--skip-git-repo-check", "-s", "read-only"):
        assert flag in argv
    assert argv[-1] == "P"                          # one argv element, not split


def test_r09_claude_summarize_flags_are_present():
    argv = actions.summarize_argv("claude", prompt="a prompt with spaces")
    assert argv[:2] == ["claude", "-p"]
    assert "--no-session-persistence" in argv
    assert argv[-1] == "a prompt with spaces"


# 10 - header overflowed: budgeted against terminal width, not pane width
def test_r10_width_comes_from_the_widget_not_the_environment(monkeypatch):
    """Structurally impossible now: Textual owns the terminal and a widget knows
    its own size, so no width has to be smuggled to a child process through the
    environment. Guard against reintroducing one."""
    import subprocess
    import sys as _sys
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "src" / "retrace"
    hits = subprocess.run(
        ["grep", "-rn", "-e", "WIDTH", "-e", "get_terminal_size", str(src)],
        capture_output=True, text=True, executable=None,
    )
    assert hits.stdout == "", hits.stdout
    assert _sys.executable                          # keeps linters quiet


# 11 - only recent sessions reachable: browse listed 400 newest messages
def test_r11_browse_lists_sessions_and_is_uncapped(indexed):
    rows = query.session_rows(indexed, limit=None)
    assert len(rows) == 3                           # every session, no cap
    # oldest session is reachable, which is what the message-based feed broke
    assert "claude-sess-2" in {r["session"] for r in rows}
    assert query.session_rows.__defaults__ is None   # keyword-only, no magic 3000


def test_r11_no_row_caps_are_hardcoded():
    """No display-limit constants in the query layer.

    A DataTable virtualises, so a cap on a listing only hides rows. The one
    remaining limit bounds query cost and is reported in the status line.
    """
    import ast
    import inspect

    from retrace import query as q
    src = inspect.getsource(q)
    tree = ast.parse(src)

    # no LIMIT baked into any SQL string - limits are bound parameters, so a
    # caller can always ask for everything
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert not __import__("re").search(r"LIMIT\s+\d", node.value), node.value

    # the only module-level numbers are a bind-parameter valve, a documented
    # query-cost bound that the UI reports, and a substring length
    module_ints = {
        t.id
        for node in tree.body if isinstance(node, ast.Assign)
        for t in node.targets if isinstance(t, ast.Name)
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, int)
    }
    assert module_ints == {"_TITLE_BIND_MAX", "FEED_LIMIT", "EXCERPT_CHARS"}, module_ints

    # and every listing honours limit=None
    assert q.session_rows.__kwdefaults__["limit"] == 40
    assert q.project_rows.__kwdefaults__["limit"] is None


def test_r11_feed_truncation_is_never_silent(indexed, monkeypatch):
    """A capped list must say so - FEED_LIMIT bounds query cost, not honesty."""
    monkeypatch.setattr(query, "FEED_LIMIT", 1)
    rows = query.message_feed(indexed, "c", limit=1)
    assert len(rows) == 1
    assert query.match_count(indexed, "c") > 1      # the UI shows n of total


# 12 - project label `-home-max-fingular-infra` -> `/home/max/fingular/infra`
def test_r12_literal_cwd_beats_the_lossy_slug(db, env):
    d = env["claude"] / "-home-max-fingular-infra"
    jsonl(d / "p.jsonl", [claude_msg("user", "hi", session="slug",
                                     cwd="/home/max/fingular-infra")])
    indexer.index(db)
    assert db.execute("SELECT DISTINCT project FROM msgs").fetchone()[0] == \
        "/home/max/fingular-infra"


def test_r12_unslug_is_only_the_fallback(env):
    from pathlib import Path
    p = env["claude"] / "-home-max-fingular-infra" / "x.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("")
    assert parsers.prescan("claude", Path(p))["project"] == "/home/max/fingular/infra"


# 13 - `--source` accepted by argparse and then never used in the query
def test_r13_source_filter_is_threaded_through(indexed):
    assert {r["source"] for r in query.message_feed(indexed, "c", source="codex")} \
        == {"codex"}
    assert {r["source"] for r in query.message_feed(indexed, "c", source="claude")} \
        == {"claude"}
    both = {r["source"] for r in query.message_feed(indexed, "c", source="all")}
    assert both == {"claude", "codex"}


def test_r13_every_filter_dimension_changes_the_result(indexed):
    base = query.message_feed(indexed, "c")
    assert len(base) > 1
    assert len(query.message_feed(indexed, "c", role="user")) < len(base)
    assert len(query.message_feed(indexed, "c", project="api")) < len(base)
    asc = query.message_feed(indexed, "c", sort="old")
    desc = query.message_feed(indexed, "c", sort="new")
    assert [r["id"] for r in asc] != [r["id"] for r in desc]


def test_md5_of_transcripts_unchanged_by_a_full_cycle(indexed, env):
    """Belt and braces for "never write to a transcript", kept next to the
    regressions it protects."""
    files = sorted(env["tmp"].rglob("*.jsonl"))
    before = {p: hashlib.md5(p.read_bytes()).hexdigest() for p in files}
    indexer.index(indexed)
    query.search(indexed, "cursor")
    query.session_rows(indexed, limit=None)
    assert {p: hashlib.md5(p.read_bytes()).hexdigest() for p in files} == before
    assert storage.fts_integrity_ok(indexed)
