"""The assumptions listed under "Things we assume and cannot verify" in AGENTS.md.

They are assumptions, not facts, so each has a fallback - and it is the fallback
that is tested here, not the assumption.
"""

from __future__ import annotations

import json

import pytest

from retrace import actions, parsers, sessions
from retrace.tui.app import RetraceApp
from conftest import claude_msg, jsonl


#: How Claude Code records `/rename`, verbatim.
RENAME_ENTRY = ("<command-name>/rename</command-name>\n"
                "            <command-message>rename</command-message>\n"
                "            <command-args>{to}</command-args>")


# 1 - keybindings in a live UI, once untestable for lack of a real terminal
def test_u1_the_bindings_are_now_actually_exercised():
    """tests/test_tui.py drives every binding through Textual's Pilot; this only
    asserts that none has been added without a test."""
    import pathlib
    src = (pathlib.Path(__file__).parent / "test_tui.py").read_text()
    for b in RetraceApp.BINDINGS:
        first = b.key.split(",")[0]
        assert f'"{first}"' in src, f"{first} has no test"


# 2 - "real less" - pager tests use stubs, so absence must degrade cleanly
def test_u2_missing_pager_writes_the_text_instead(monkeypatch):
    monkeypatch.setenv("RETRACE_PAGER", "no-pager-here")
    got = []
    actions.page("the session text", fallback=got.append)
    assert got == ["the session text"]


def test_u2_pager_flags_are_only_added_for_less(monkeypatch):
    monkeypatch.setenv("RETRACE_PAGER", "less")
    assert actions.pager_argv(7)[-2:] == ["-R", "+7"] if actions.pager_argv(7) else True
    monkeypatch.setenv("RETRACE_PAGER", "bat --paging=always")
    argv = actions.pager_argv(7)
    if argv:                            # only if bat happens to be installed
        assert "-R" not in argv and "+7" not in argv


def test_u2_suspend_failure_leaves_a_working_app(indexed, monkeypatch):
    """If Textual's suspend/restore breaks - the reason the dependency is pinned
    exactly - the app reports it rather than dying with the terminal in raw
    mode."""
    monkeypatch.setattr(RetraceApp, "suspend",
                        lambda self: (_ for _ in ()).throw(RuntimeError("no tty")))
    app = RetraceApp(indexed)
    notes: list[str] = []
    monkeypatch.setattr(RetraceApp, "notify",
                        lambda self, msg, **kw: notes.append(msg))
    app.run_external(lambda: None)
    assert notes and "could not hand over the terminal" in notes[0]


# 3 - "`codex resume <id>` is the correct invocation"
def test_u3_resume_command_is_a_template(indexed, monkeypatch):
    """If `codex resume <id>` turns out to be wrong, it is one env var away from
    being right, and the id we pass is the one from session_meta."""
    r = sessions.session_by_id(indexed, "codex-real-id")
    assert actions.resume_argv(r) == ["codex", "resume", "codex-real-id"]
    monkeypatch.setenv("RETRACE_CODEX_RESUME", "codex --resume-session {id}")
    assert actions.resume_argv(r) == ["codex", "--resume-session", "codex-real-id"]


# 4 - "whether cli_name exists in any current transcript format"
def test_u4_absent_cli_name_is_not_an_error(db, env):
    """Nothing depends on the name being there. The prescan returns None and the
    title falls back to the first user message."""
    from retrace import indexer, query
    jsonl(env["claude"] / "-a" / "a.jsonl",
          [claude_msg("user", "no cli name in this file", session="noname")])
    indexer.index(db)
    assert db.execute("SELECT cli_name FROM files").fetchone()[0] is None
    assert query.session_rows(db)[0]["title"] == "no cli name in this file"


def test_u4_a_cli_name_is_used_when_present(db, env):
    from retrace import indexer, query
    jsonl(env["claude"] / "-b" / "b.jsonl", [
        {"type": "user", "sessionId": "named", "uuid": "u1",
         "cwd": "/tmp/b", "sessionName": "the cli's own name",
         "message": {"role": "user", "content": "hello"}},
    ])
    indexer.index(db)
    assert query.session_rows(db)[0]["title"] == "the cli's own name"


def test_u4_a_later_rename_wins(db, env):
    """`/rename` is recorded in the conversation, and renames append."""
    from retrace import indexer, query

    def rename(to: str, ts: str) -> str:
        return RENAME_ENTRY.format(to=to)

    jsonl(env["claude"] / "-c" / "c.jsonl", [
        claude_msg("user", "start of the work", session="ren"),
        claude_msg("user", rename("first-name", "t1"), session="ren"),
        claude_msg("assistant", "carrying on", session="ren"),
        claude_msg("user", rename("renamed-later", "t2"), session="ren"),
    ])
    indexer.index(db)
    assert db.execute("SELECT cli_name FROM files").fetchone()[0] == "renamed-later"
    assert query.session_rows(db)[0]["title"] == "renamed-later"


def test_u4_a_rename_late_in_a_big_file_is_still_found(db, env):
    """The prescan reads a bounded head of the file; a rename usually happens once
    the session has a subject, which is not in the first few hundred KB."""
    from retrace import indexer, query

    filler = [claude_msg("user", "x" * 2000, session="late") for _ in range(300)]
    jsonl(env["claude"] / "-d" / "d.jsonl", filler + [
        claude_msg("user", RENAME_ENTRY.format(to="found-at-the-end"),
                   session="late"),
    ])
    assert (env["claude"] / "-d" / "d.jsonl").stat().st_size > 600_000
    indexer.index(db)
    assert query.session_rows(db)[0]["title"] == "found-at-the-end"


def test_u4_an_append_without_a_rename_keeps_the_name(db, env):
    """An incremental pass reads only the delta. Finding no rename in it means
    "no change", not "the name is gone"."""
    from retrace import indexer, query

    p = env["claude"] / "-e" / "e.jsonl"
    jsonl(p, [claude_msg("user", RENAME_ENTRY.format(to="keep-me"),
                         session="keep")])
    indexer.index(db)
    assert query.session_rows(db)[0]["title"] == "keep-me"

    jsonl(p, [claude_msg("user", "more work, no rename", session="keep")],
          append=True)
    indexer.index(db)
    assert db.execute("SELECT cli_name FROM files").fetchone()[0] == "keep-me"
    assert query.session_rows(db)[0]["title"] == "keep-me"


def test_u4_rename_parsing(env):
    assert parsers.rename_from_text(
        "<command-name>/rename</command-name>\n"
        "            <command-message>rename</command-message>\n"
        "            <command-args>onprem-failover</command-args>") == "onprem-failover"
    assert parsers.rename_from_text("just a message about /rename") is None
    assert parsers.rename_from_text("") is None
    assert parsers.rename_from_text(
        "<command-name>/model</command-name><command-args>opus</command-args>") is None


# 5 - "whether `codex exec` uses stdin as context"
def test_u5_summarize_failure_is_surfaced_not_swallowed(indexed, stubs):
    """We cannot verify that codex reads our stdin as context. What we can do is
    never hide a failure, and never label a session with a non-answer."""
    stubs.install("codex", "sys.stderr.write('I do not read stdin'); sys.exit(1)")
    r = sessions.session_by_id(indexed, "codex-real-id")
    with pytest.raises(actions.ActionError) as e:
        actions.summarize_session(indexed, r)
    assert "exited 1" in str(e.value)
    assert sessions.get_label(indexed, "codex-real-id") is None


def test_u5_summary_which_flag_lets_you_switch_cli(indexed, stubs):
    """If one CLI turns out not to read stdin, the other is one flag away."""
    stubs.install("claude", "sys.stdout.write('claude did the work')")
    r = sessions.session_by_id(indexed, "codex-real-id")
    assert actions.summarize_session(indexed, r, which="claude") == "claude did the work"


def test_u5_transcript_actually_reaches_the_child(indexed, stubs):
    """The part we *can* verify: the full rendered session is on the child's
    stdin, plain, with the session id in it."""
    stubs.install("codex", "sys.stdout.write('ok')")
    r = sessions.session_by_id(indexed, "codex-real-id")
    actions.summarize_session(indexed, r)
    sent = stubs.calls("codex")[0]["stdin"]
    assert "keyset cursor" in sent and "pagination" in sent
    assert "codex-real-id" in sent


def test_no_dataclasses_for_transcript_shapes():
    """Requirement, and the reason for all of the above: the transcript format is
    internal to both CLIs and reshaped between releases."""
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(parsers))
    decorators = {
        d.id if isinstance(d, ast.Name) else getattr(d, "attr", "")
        for node in ast.walk(tree) if isinstance(node, ast.ClassDef)
        for d in node.decorator_list
    }
    assert not decorators & {"dataclass", "define", "frozen"}, decorators
    bases = {
        b.id if isinstance(b, ast.Name) else getattr(b, "attr", "")
        for node in ast.walk(tree) if isinstance(node, ast.ClassDef)
        for b in node.bases
    }
    assert not bases & {"TypedDict", "BaseModel", "NamedTuple"}, bases


def test_u4_codex_names_come_from_its_own_index(db, env):
    """Codex keeps renames in a sibling session_index.jsonl, not in the
    transcript. Verified against a real one - this was the "may live in a sibling
    index file" case, and for Codex it does."""
    from retrace import indexer, query

    p = (env["codex"] / "2026" / "07" / "16" /
         "rollout-2026-07-16T10-57-51-019f69ee-93ad-7010-9328-9b397c6b1ad8.jsonl")
    jsonl(p, [{"timestamp": "2026-07-16T10:57:51Z", "type": "session_meta",
               "payload": {"type": "session_meta",
                           "id": "019f69ee-93ad-7010-9328-9b397c6b1ad8",
                           "cwd": "/work/lamoda_webhooks"}},
              {"timestamp": "2026-07-16T10:58:00Z", "type": "response_item",
               "payload": {"type": "message", "role": "user",
                           "content": [{"type": "input_text",
                                        "text": "fix the data import"}]}}])
    (env["codex"].parent / "session_index.jsonl").write_text(
        json.dumps({"id": "019f69ee-93ad-7010-9328-9b397c6b1ad8",
                    "thread_name": "thecultt-data-fixes",
                    "updated_at": "2026-07-28T21:55:46Z"}) + "\n")

    rep = indexer.index(db)
    assert rep.named == 1
    assert query.session_rows(db)[0]["title"] == "thecultt-data-fixes"


def test_u4_a_later_codex_rename_wins(db, env):
    from retrace import indexer, query

    p = env["codex"] / "rollout-2026-07-16T10-00-00-aaaa1111-2222-3333-4444-555555555555.jsonl"
    jsonl(p, [{"type": "session_meta",
               "payload": {"type": "session_meta", "id": "sid-1", "cwd": "/w"}},
              {"type": "response_item",
               "payload": {"type": "message", "role": "user",
                           "content": [{"type": "input_text", "text": "hello"}]}}])
    idx = env["codex"].parent / "session_index.jsonl"
    idx.write_text(json.dumps({"id": "sid-1", "thread_name": "first"}) + "\n"
                   + json.dumps({"id": "sid-1", "thread_name": "renamed"}) + "\n")
    indexer.index(db)
    assert query.session_rows(db)[0]["title"] == "renamed"


def test_u4_a_missing_or_broken_codex_index_is_not_an_error(db, env):
    from retrace import indexer

    p = env["codex"] / "rollout-2026-07-16T10-00-00-bbbb1111-2222-3333-4444-555555555555.jsonl"
    jsonl(p, [{"type": "session_meta",
               "payload": {"type": "session_meta", "id": "sid-2", "cwd": "/w"}},
              {"type": "response_item",
               "payload": {"type": "message", "role": "user",
                           "content": [{"type": "input_text", "text": "hello"}]}}])
    assert indexer.index(db).named == 0            # no index file at all

    (env["codex"].parent / "session_index.jsonl").write_text(
        "not json\n[]\n{\"id\": 7}\n" + json.dumps({"thread_name": "no id"}) + "\n")
    assert indexer.index(db).named == 0            # every line unusable


def test_u4_codex_index_location_is_overridable(env, monkeypatch, tmp_path):
    from retrace import config

    assert config.codex_index_path() == env["codex"].parent / "session_index.jsonl"
    monkeypatch.setenv("RETRACE_CODEX_INDEX", str(tmp_path / "elsewhere.jsonl"))
    assert config.codex_index_path() == tmp_path / "elsewhere.jsonl"
