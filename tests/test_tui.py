"""The browser, driven headlessly through Textual's Pilot.

Every keybinding is exercised here. A TUI whose bindings are only ever tested by
hand is a TUI with untested bindings.
"""

from __future__ import annotations

import os

import pytest

from retrace import actions, query, sessions
from retrace.tui.app import RetraceApp, highlight, run_app, short_project


def text_of(widget) -> str:
    """Plain text of a Static. Textual 8 exposes `.content`, not `.renderable`."""
    return str(widget.content)


def app_for(db, **kw) -> RetraceApp:
    return RetraceApp(db, **kw)


async def settle(pilot):
    await pilot.pause(0.15)          # past the input debounce
    await pilot.pause()


# --------------------------------------------------------------------- listings

async def test_opens_on_sessions_newest_first(indexed):
    app = app_for(indexed)
    async with app.run_test() as pilot:
        await settle(pilot)
        table = app.query_one("#table")
        assert table.row_count == 3
        assert "codex-real-id" in [r["session"] for r in app.rows.values()][:1]
        assert "3 sessions" in text_of(app.query_one("#status"))


async def test_typing_switches_to_message_search(indexed):
    app = app_for(indexed)
    async with app.run_test() as pilot:
        await pilot.press(*"curso")          # a partial word, mid-typing
        await settle(pilot)
        assert [r["kind"] for r in app.rows.values()] == ["message", "message"]
        assert "matching messages" in text_of(app.query_one("#status"))


async def test_escape_clears_and_never_exits(indexed):
    """Escape is the key you hit after a mistyped search. Quitting the whole
    application on it, just because the box happened to be empty, is a coin flip
    on something you cannot undo."""
    app = app_for(indexed, query_text="cursor")
    async with app.run_test() as pilot:
        await settle(pilot)
        await pilot.press("escape")
        await settle(pilot)
        assert app.query_text == ""
        assert [r["kind"] for r in app.rows.values()] == ["session"] * 3

        await pilot.press("escape")          # again, with nothing to clear
        await settle(pilot)
        assert app.is_running


async def test_escape_clears_filters_before_giving_up(indexed):
    app = app_for(indexed)
    async with app.run_test() as pilot:
        await settle(pilot)
        await pilot.press("alt+c")           # source=claude
        await pilot.press("alt+u")           # role=user
        await settle(pilot)
        await pilot.press("escape")
        await settle(pilot)
        assert (app.source, app.role, app.view) == ("all", "all", "sessions")
        assert app.is_running


async def test_ctrl_q_quits(indexed):
    app = app_for(indexed, query_text="cursor")
    async with app.run_test() as pilot:
        await settle(pilot)
        await pilot.press("ctrl+q")
        await pilot.pause()
    assert app.return_value is None


async def test_ctrl_c_and_ctrl_x_belong_to_the_search_box(indexed):
    """They are copy and cut in a focused Input, which is why ctrl+c does not quit
    while typing and why delete is not bound to ctrl+x."""
    from textual.widgets import Input
    owned = {k for b in Input.BINDINGS for k in b.key.split(",")}
    assert {"ctrl+c", "ctrl+x"} <= owned

    app = app_for(indexed, query_text="cursor")
    async with app.run_test() as pilot:
        await settle(pilot)
        await pilot.press("ctrl+x")          # cut, not delete-session
        await settle(pilot)
        assert app.is_running and len(app.screen_stack) == 1
        await pilot.press("ctrl+c")          # copy, not quit
        await pilot.pause()
        assert app.is_running


async def test_bad_query_does_not_crash(indexed):
    app = app_for(indexed)
    async with app.run_test() as pilot:
        await pilot.press(*'NEAR("x')
        await settle(pilot)
        assert app.query_one("#table").row_count == 1
        assert app.rows == {}


# ---------------------------------------------------------------------- filters

async def test_source_filter_keys(indexed):
    app = app_for(indexed)
    async with app.run_test() as pilot:
        await settle(pilot)
        await pilot.press("alt+c")
        await settle(pilot)
        assert {r["source"] for r in app.rows.values()} == {"claude"}
        await pilot.press("alt+x")
        await settle(pilot)
        assert {r["source"] for r in app.rows.values()} == {"codex"}
        await pilot.press("alt+a")
        await settle(pilot)
        assert {r["source"] for r in app.rows.values()} == {"claude", "codex"}


async def test_role_cycle_key(indexed):
    app = app_for(indexed, query_text="c")
    async with app.run_test() as pilot:
        await settle(pilot)
        assert app.role == "all"
        await pilot.press("alt+u")
        await settle(pilot)
        assert app.role == "user"
        assert {r["role"] for r in app.rows.values() if r["kind"] == "message"} \
            <= {"user", "user/sub"}
        await pilot.press("alt+u")
        await settle(pilot)
        assert app.role == "assistant"
        await pilot.press("alt+u")
        await settle(pilot)
        assert app.role == "all"


async def test_sort_flip_key(indexed):
    app = app_for(indexed)
    async with app.run_test() as pilot:
        await settle(pilot)
        newest = list(app.rows)
        await pilot.press("alt+d")
        await settle(pilot)
        assert app.sort == "old"
        assert list(app.rows) == list(reversed(newest))
        assert "oldest first" in text_of(app.query_one("#status"))


async def test_folder_level_and_descent(indexed):
    app = app_for(indexed)
    async with app.run_test() as pilot:
        await settle(pilot)
        await pilot.press("alt+p")
        await settle(pilot)
        assert app.view == "projects"
        assert all(r["kind"] == "project" for r in app.rows.values())

        # enter on a folder descends rather than resuming
        await pilot.press("enter")
        await settle(pilot)
        assert app.view == "sessions"
        assert app.project
        assert all(r["project"] == app.project for r in app.rows.values())
        assert app.return_value is None

        # going back up clears the folder filter
        await pilot.press("alt+p")
        await settle(pilot)
        assert app.project == ""


async def test_folder_view_query_filters_paths_not_text(indexed):
    app = app_for(indexed, view="projects")
    async with app.run_test() as pilot:
        await settle(pilot)
        await pilot.press(*"api")
        await settle(pilot)
        assert [r["project"] for r in app.rows.values()] == ["/home/max/projects/api"]


async def test_cursor_keys_move_the_selection(indexed):
    app = app_for(indexed)
    async with app.run_test() as pilot:
        await settle(pilot)
        first = app.current()["session"]
        await pilot.press("down")
        await pilot.pause()
        assert app.current()["session"] != first
        await pilot.press("up")
        await pilot.pause()
        assert app.current()["session"] == first


async def test_page_keys_move_the_selection(indexed):
    app = app_for(indexed)
    async with app.run_test(size=(100, 12)) as pilot:
        await settle(pilot)
        first = app.current()["session"]
        await pilot.press("pagedown")
        await pilot.pause()
        assert app.current()["session"] != first
        await pilot.press("pageup")
        await pilot.pause()
        assert app.current()["session"] == first


# ---------------------------------------------------------------------- preview

async def test_preview_follows_the_selection(indexed):
    app = app_for(indexed)
    async with app.run_test() as pilot:
        await settle(pilot)
        text = text_of(app.query_one("#preview-content"))
        assert "keyset cursor" in text          # newest session is the codex one
        await pilot.press("down")
        await pilot.pause()
        assert "mikrotik" in text_of(app.query_one("#preview-content"))


async def test_message_preview_marks_the_hit(indexed):
    app = app_for(indexed, query_text="conntrack")
    async with app.run_test() as pilot:
        await settle(pilot)
        text = text_of(app.query_one("#preview-content"))
        assert ">>>" in text and "conntrack" in text


async def test_folder_preview_lists_sessions(indexed):
    app = app_for(indexed, view="projects")
    async with app.run_test() as pilot:
        await settle(pilot)
        assert "enter to descend" in text_of(app.query_one("#preview-content"))


# ----------------------------------------------------------------------- resume

async def test_enter_hands_back_a_resume(indexed):
    app = app_for(indexed)
    async with app.run_test() as pilot:
        await settle(pilot)
        await pilot.press("enter")
        await pilot.pause()
    what, row = app.return_value
    assert what == "resume" and row["session"] == "codex-real-id"


async def test_run_app_execs_after_the_app_exits(indexed, monkeypatch, capsys):
    """resume must happen *after* Textual releases the terminal, or execvp
    inherits raw mode."""
    order = []
    monkeypatch.setattr(RetraceApp, "run", lambda self: order.append("app") or
                        ("resume", sessions.session_by_id(indexed, "claude-sess-1")))
    monkeypatch.setattr(actions, "do_resume",
                        lambda r, dry_run=False: order.append("resume"))
    assert run_app(indexed, _Args()) == 0
    assert order == ["app", "resume"]
    assert "claude --resume claude-sess-1" in capsys.readouterr().out


async def test_run_app_reports_a_resume_failure(indexed, monkeypatch, capsys):
    monkeypatch.setattr(RetraceApp, "run", lambda self: (
        "resume", sessions.session_by_id(indexed, "claude-sess-1")))
    monkeypatch.setenv("RETRACE_CLAUDE_RESUME", "no-such-binary-at-all {id}")
    assert run_app(indexed, _Args()) == 1
    assert "not on PATH" in capsys.readouterr().out


class _Args:
    query = ""
    source = None
    role = None
    project = None
    sort = None
    projects = False
    dry_run = False
    no_index = True


# ------------------------------------------------------------------ print / page

async def test_ctrl_o_prints_the_message(indexed):
    app = app_for(indexed, query_text="conntrack")
    async with app.run_test() as pilot:
        await settle(pilot)
        await pilot.press("ctrl+o")
        await pilot.pause()
    what, text = app.return_value
    assert what == "print" and "conntrack" in text


async def test_ctrl_o_on_a_session_row_prints_the_session(indexed):
    app = app_for(indexed)
    async with app.run_test() as pilot:
        await settle(pilot)
        await pilot.press("ctrl+o")
        await pilot.pause()
    what, text = app.return_value
    assert what == "print" and "keyset cursor" in text


async def test_ctrl_s_pages_the_session(indexed, monkeypatch):
    import contextlib
    # headless: there is no terminal to hand over, so stub the handover itself
    monkeypatch.setattr(RetraceApp, "suspend", lambda self: contextlib.nullcontext())
    seen = {}
    monkeypatch.setattr(actions, "page",
                        lambda text, jump=1, **kw: seen.update(text=text, jump=jump))
    app = app_for(indexed, query_text="conntrack")
    async with app.run_test() as pilot:
        await settle(pilot)
        await pilot.press("ctrl+s")
        await pilot.pause()
    assert "conntrack" in seen["text"]
    assert seen["jump"] > 1                     # positioned on the hit


async def test_suspend_failure_is_reported_not_fatal(indexed, monkeypatch):
    """The pinned-Textual failure mode, exercised.

    If suspend/restore breaks, the user gets an error toast and a working app -
    not a traceback and a wedged terminal.
    """
    def boom(self):
        raise RuntimeError("suspend not supported here")

    monkeypatch.setattr(RetraceApp, "suspend", boom)
    notes = []
    app = app_for(indexed)
    async with app.run_test() as pilot:
        await settle(pilot)
        monkeypatch.setattr(type(app), "notify",
                            lambda self, msg, **kw: notes.append((msg, kw)))
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert app.is_running                   # still alive
    assert notes and "could not hand over the terminal" in notes[0][0]
    assert notes[0][1]["severity"] == "error"


async def test_external_child_runs_inside_suspend(indexed, monkeypatch):
    """The pager must be launched while the app is suspended, not before."""
    events = []

    class FakeSuspend:
        def __enter__(self):
            events.append("suspend")

        def __exit__(self, *a):
            events.append("resume")
            return False

    monkeypatch.setattr(RetraceApp, "suspend", lambda self: FakeSuspend())
    monkeypatch.setattr(actions, "page", lambda *a, **kw: events.append("pager"))
    app = app_for(indexed)
    async with app.run_test() as pilot:
        await settle(pilot)
        await pilot.press("ctrl+s")
        await pilot.pause()
    assert events == ["suspend", "pager", "resume"]


# ---------------------------------------------------------------------- renaming

async def test_ctrl_r_labels_a_session(indexed):
    app = app_for(indexed)
    async with app.run_test() as pilot:
        await settle(pilot)
        sess = app.current()["session"]
        await pilot.press("ctrl+r")
        await pilot.pause()
        await pilot.press(*"my label")
        await pilot.press("enter")
        await settle(pilot)
        assert sessions.get_label(indexed, sess) == "my label"
        assert any(r.get("label") == "my label" for r in app.rows.values())


async def test_rename_with_empty_input_clears(indexed):
    sessions.set_label(indexed, "codex-real-id", "old label")
    app = app_for(indexed)
    async with app.run_test() as pilot:
        await settle(pilot)
        await pilot.press("ctrl+r")
        await pilot.pause()
        for _ in range("old label".__len__()):
            await pilot.press("backspace")
        await pilot.press("enter")
        await settle(pilot)
    assert sessions.get_label(indexed, "codex-real-id") is None


async def test_rename_escape_keeps_the_label(indexed):
    sessions.set_label(indexed, "codex-real-id", "keep me")
    app = app_for(indexed)
    async with app.run_test() as pilot:
        await settle(pilot)
        await pilot.press("ctrl+r")
        await pilot.pause()
        await pilot.press("escape")
        await settle(pilot)
    assert sessions.get_label(indexed, "codex-real-id") == "keep me"


# ---------------------------------------------------------------------- deleting

async def test_f8_cancel_keeps_everything(indexed):
    path = sessions.session_paths(indexed, "codex-real-id")[0]
    app = app_for(indexed)
    async with app.run_test() as pilot:
        await settle(pilot)
        await pilot.press("f8")
        await pilot.pause()
        await pilot.press("escape")
        await settle(pilot)
    assert os.path.exists(path)
    assert indexed.execute("SELECT count(*) FROM msgs WHERE session='codex-real-id'"
                           ).fetchone()[0] == 2


async def test_f8_index_only(indexed):
    path = sessions.session_paths(indexed, "codex-real-id")[0]
    app = app_for(indexed)
    async with app.run_test() as pilot:
        await settle(pilot)
        await pilot.press("f8")
        await pilot.pause()
        await pilot.press("i")
        await settle(pilot)
        assert app.query_one("#table").row_count == 2
    assert os.path.exists(path)
    from retrace import storage
    assert storage.fts_integrity_ok(indexed)


async def test_f8_purge_deletes_the_file(indexed):
    path = sessions.session_paths(indexed, "codex-real-id")[0]
    app = app_for(indexed)
    async with app.run_test() as pilot:
        await settle(pilot)
        await pilot.press("f8")
        await pilot.pause()
        await pilot.press("y")
        await settle(pilot)
    assert not os.path.exists(path)
    from retrace import storage
    assert storage.fts_integrity_ok(indexed)


async def test_delete_is_not_next_to_a_filter_key(indexed):
    """alt+x filters to codex; a slip must not be able to reach deletion."""
    keys = {b.key: b.action for b in RetraceApp.BINDINGS}
    assert keys["f8"] == "delete"
    assert "delete" not in keys.get("ctrl+x", "")
    assert "delete" not in keys.get("ctrl+d", "")
    assert keys["alt+x"] == "source('codex')"

    # and no binding may collide with an Input binding, or the focused search box
    # eats it before the app sees it
    from textual.widgets import Input
    taken = {k for b in Input.BINDINGS for k in b.key.split(",")}
    ours = {k for b in RetraceApp.BINDINGS for k in b.key.split(",")}
    assert not (ours & taken) - {"enter", "escape"}, ours & taken


# ------------------------------------------------------------------ summarising

async def test_alt_s_summarises_and_offers_the_label(indexed, stubs):
    stubs.install("codex", "sys.stdout.write('It was about cursors.')")
    app = app_for(indexed)
    async with app.run_test() as pilot:
        await settle(pilot)
        await pilot.press("alt+s")
        # The summary runs in a thread worker, so wait on the widget rather than
        # on a frame count or on the screen: a pushed screen is not a composed
        # one, and waiting for the screen alone made this test flaky.
        content = None
        for _ in range(300):
            await pilot.pause(0.05)
            if len(app.screen_stack) > 1:
                found = app.screen_stack[-1].query("#text-content")
                if found:
                    content = found.first()
                    break
        assert content is not None, "the summary screen never appeared"
        body = text_of(content)
        assert "It was about cursors." in body
        await pilot.press("l")                  # save the summary as the label
        await settle(pilot)
    assert sessions.get_label(indexed, "codex-real-id") == "It was about cursors."


async def test_summarize_failure_notifies(indexed, monkeypatch):
    monkeypatch.setenv("RETRACE_CODEX_SUMMARIZE", "no-such-cli {prompt}")
    notes = []
    app = app_for(indexed)
    async with app.run_test() as pilot:
        await settle(pilot)
        monkeypatch.setattr(type(app), "notify",
                            lambda self, msg, **kw: notes.append(msg))
        await pilot.press("alt+s")
        for _ in range(300):
            await pilot.pause(0.05)
            if any("not on PATH" in n for n in notes):
                break
    assert any("not on PATH" in n for n in notes)


# --------------------------------------------------------------------- key help

async def test_f1_shows_help(indexed):
    app = app_for(indexed)
    async with app.run_test() as pilot:
        await settle(pilot)
        await pilot.press("f1")
        await pilot.pause()
        body = text_of(app.screen_stack[-1].query_one("#text-content"))
        assert "f8" in body and "alt+u" in body
        assert "ctrl+q" in body and "ctrl+c" in body
        await pilot.press("escape")
        await pilot.pause()
        assert len(app.screen_stack) == 1


async def test_help_documents_every_binding(indexed):
    from retrace.tui.app import HELP
    for b in RetraceApp.BINDINGS:
        if not b.show and b.key not in ("up", "down"):
            continue
        key = b.key.split(",")[0]
        assert key in HELP or key in ("enter", "escape", "f1"), key


# ------------------------------------------------------------------- unit bits

def test_short_project():
    assert short_project("/home/max/projects/infra") == "infra"
    assert short_project(None) == "-"
    assert short_project("/a/very/long/directory-name-that-goes-on") == \
        "directory-name-tha"


def test_highlight_marks_the_term():
    text = highlight("some conntrack text", 5, "conntrack")
    assert any(span.style == "bold yellow" for span in text.spans)
    assert not highlight("no match here", -1, "x").spans


async def test_empty_row_actions_are_no_ops(indexed):
    """Actions on an empty list return silently rather than raising."""
    app = app_for(indexed, query_text="zzzzzznotfound")
    async with app.run_test() as pilot:
        await settle(pilot)
        assert app.current() is None
        for key in ("enter", "ctrl+o", "ctrl+s", "ctrl+r", "f8", "alt+s"):
            await pilot.press(key)
            await pilot.pause()
        assert app.is_running and len(app.screen_stack) == 1


@pytest.mark.parametrize("kw", [{"source": "codex"}, {"role": "user"},
                                {"project": "api"}, {"sort": "old"}])
async def test_initial_state_from_flags(indexed, kw):
    app = app_for(indexed, query_text="c", **kw)
    async with app.run_test() as pilot:
        await settle(pilot)
        assert app.rows


async def test_folder_descent_is_exact_not_a_substring(db, env):
    """/work/api and /work/api-old are different folders. Descending into one
    must not list the other's sessions."""
    from retrace import indexer
    from conftest import claude_msg, jsonl

    jsonl(env["claude"] / "-work-api" / "a.jsonl",
          [claude_msg("user", "the api session", session="api-1", cwd="/work/api")])
    jsonl(env["claude"] / "-work-api-old" / "b.jsonl",
          [claude_msg("user", "the old api session", session="api-2",
                      cwd="/work/api-old", ts="2026-06-02T00:00:00Z")])
    indexer.index(db)

    app = app_for(db, view="projects")
    async with app.run_test() as pilot:
        await settle(pilot)
        # select the /work/api row, whichever position it is in
        for _ in range(app.query_one("#table").row_count):
            if app.current()["project"] == "/work/api":
                break
            await pilot.press("down")
            await pilot.pause()
        assert app.current()["project"] == "/work/api"

        await pilot.press("enter")
        await settle(pilot)
        assert app.project == "/work/api" and app.project_exact
        assert {r["session"] for r in app.rows.values()} == {"api-1"}


async def test_folder_descent_is_exact_for_message_search_too(db, env):
    from retrace import indexer
    from conftest import claude_msg, jsonl

    jsonl(env["claude"] / "-work-api" / "a.jsonl",
          [claude_msg("user", "shared keyword here", session="api-1", cwd="/work/api")])
    jsonl(env["claude"] / "-work-api-old" / "b.jsonl",
          [claude_msg("user", "shared keyword there", session="api-2",
                      cwd="/work/api-old")])
    indexer.index(db)

    app = app_for(db, view="projects")
    async with app.run_test() as pilot:
        await settle(pilot)
        while app.current()["project"] != "/work/api":
            await pilot.press("down")
            await pilot.pause()
        await pilot.press("enter")
        await settle(pilot)
        await pilot.press(*"keyword")
        await settle(pilot)
        assert {r["session"] for r in app.rows.values()} == {"api-1"}


async def test_a_project_flag_stays_a_substring(db, env):
    """The flag is something a user typed, so partial paths must still work."""
    from retrace import indexer
    from conftest import claude_msg, jsonl

    jsonl(env["claude"] / "-work-api" / "a.jsonl",
          [claude_msg("user", "one", session="api-1", cwd="/work/api")])
    jsonl(env["claude"] / "-work-api-old" / "b.jsonl",
          [claude_msg("user", "two", session="api-2", cwd="/work/api-old")])
    indexer.index(db)

    app = app_for(db, project="api")
    async with app.run_test() as pilot:
        await settle(pilot)
        assert app.project_exact is False
        assert {r["session"] for r in app.rows.values()} == {"api-1", "api-2"}


async def test_going_up_clears_exactness(db, env):
    from retrace import indexer
    from conftest import claude_msg, jsonl

    jsonl(env["claude"] / "-work-api" / "a.jsonl",
          [claude_msg("user", "one", session="api-1", cwd="/work/api")])
    indexer.index(db)
    app = app_for(db, view="projects")
    async with app.run_test() as pilot:
        await settle(pilot)
        await pilot.press("enter")
        await settle(pilot)
        assert app.project_exact
        await pilot.press("alt+p")
        await settle(pilot)
        assert app.project == "" and app.project_exact is False


# ------------------------------------------------------- key discoverability

def test_the_footer_never_silently_drops_a_binding():
    """Textual's Footer is a fixed list that truncates. Anything that does not
    fit has to be somewhere else on screen, not merely in the help."""
    shown = [b for b in RetraceApp.BINDINGS if b.show]
    # the footer renders the first key of a multi-key binding, so that is what
    # costs columns
    budget = sum(len(b.key.split(",")[0]) + len(b.description) + 4 for b in shown)
    assert budget <= 100, (
        f"{budget} columns of footer: it will truncate on a normal terminal. "
        "Move something to the status line."
    )


@pytest.mark.parametrize("width", [140, 118, 100, 80])
async def test_filter_keys_are_on_screen_at_any_width(indexed, width):
    app = app_for(indexed)
    async with app.run_test(size=(width, 14)) as pilot:
        await settle(pilot)
        status = text_of(app.query_one("#status"))
        assert len(status) <= width
        assert "alt+c claude" in status or "alt+x codex" in status


async def test_the_status_legend_names_where_a_key_takes_you(indexed):
    app = app_for(indexed)
    async with app.run_test(size=(140, 14)) as pilot:
        await settle(pilot)
        status = text_of(app.query_one("#status"))
        # sorted newest first, so the sort key offers "oldest"
        assert "alt+d oldest" in status
        assert "alt+c claude" in status and "alt+x codex" in status
        assert "alt+a both" not in status        # already showing both

        await pilot.press("alt+d")
        await settle(pilot)
        assert "alt+d newest" in text_of(app.query_one("#status"))

        await pilot.press("alt+c")
        await settle(pilot)
        status = text_of(app.query_one("#status"))
        assert "alt+a both" in status and "alt+c claude" not in status


async def test_the_role_legend_cycles(indexed):
    app = app_for(indexed)
    async with app.run_test(size=(140, 14)) as pilot:
        await settle(pilot)
        assert "alt+u user" in text_of(app.query_one("#status"))
        await pilot.press("alt+u")
        await settle(pilot)
        assert "alt+u assistant" in text_of(app.query_one("#status"))
        await pilot.press("alt+u")
        await settle(pilot)
        assert "alt+u any role" in text_of(app.query_one("#status"))


async def test_no_role_legend_at_folder_level(indexed):
    """Roles are meaningless when the rows are folders."""
    app = app_for(indexed, view="projects")
    async with app.run_test(size=(140, 14)) as pilot:
        await settle(pilot)
        assert "alt+u" not in text_of(app.query_one("#status"))


def test_fit_drops_whole_entries():
    from retrace.tui.app import fit
    assert fit(["aaa", "bbb", "ccc"], 100) == "aaa · bbb · ccc"
    assert fit(["aaa", "bbb", "ccc"], 10) == "aaa · bbb"
    assert fit(["aaa"], 1) == "aaa"          # never returns nothing


async def test_typing_a_session_name_finds_the_session(db, env):
    """Names are outside the FTS index, so a name search is a different query.
    Without the fallback, typing the name of a session you renamed finds nothing."""
    from retrace import indexer
    from conftest import claude_msg, claude_rename, jsonl

    jsonl(env["claude"] / "-a" / "a.jsonl", [
        claude_msg("user", "the conversation itself never says that", session="n1"),
        claude_rename("data-fixes", session="n1"),
    ])
    indexer.index(db)

    app = app_for(db)
    async with app.run_test() as pilot:
        await pilot.press(*"data-fix")
        await settle(pilot)
        kinds = [r["kind"] for r in app.rows.values()]
        assert kinds == ["session"]
        assert next(iter(app.rows.values()))["title"] == "data-fixes"
        assert "1 named session" in text_of(app.query_one("#status"))


async def test_a_named_session_appears_above_the_messages(indexed):
    """Both, and the session first. A session called `conntrack` must not vanish
    from the results just because messages also mention conntrack - that is exactly
    how a name-only fallback fails."""
    sessions.set_label(indexed, "claude-sess-1", "conntrack notes")
    app = app_for(indexed)
    async with app.run_test() as pilot:
        await pilot.press(*"conntrack")
        await settle(pilot)
        kinds = [r["kind"] for r in app.rows.values()]
        assert kinds[0] == "session" and "message" in kinds
        status = text_of(app.query_one("#status"))
        assert "1 named session" in status and "matching messages" in status


async def test_a_named_row_and_a_message_row_can_coexist(indexed):
    """The same session on both sides of the list must not collide as one row."""
    sessions.set_label(indexed, "claude-sess-1", "conntrack notes")
    app = app_for(indexed)
    async with app.run_test() as pilot:
        await pilot.press(*"conntrack")
        await settle(pilot)
        assert app.query_one("#table").row_count == len(app.rows)
        sess_rows = [r for r in app.rows.values() if r["kind"] == "session"]
        msg_rows = [r for r in app.rows.values() if r["kind"] == "message"]
        assert len(sess_rows) == 1 and len(msg_rows) == 1
        assert sess_rows[0]["session"] == msg_rows[0]["session"]


async def test_a_name_match_is_still_resumable(db, env):
    from retrace import indexer
    from conftest import claude_msg, claude_rename, jsonl

    jsonl(env["claude"] / "-a" / "a.jsonl",
          [claude_msg("user", "some work happened", session="n2"),
           claude_rename("named-one", session="n2")])
    indexer.index(db)
    app = app_for(db)
    async with app.run_test() as pilot:
        await pilot.press(*"named-one")
        await settle(pilot)
        await pilot.press("enter")
        await pilot.pause()
    what, row = app.return_value
    assert what == "resume" and row["session"] == "n2"
