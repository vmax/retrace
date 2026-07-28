from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from retrace import cli, sessions

SRC = str(Path(__file__).resolve().parents[1] / "src")


def run_cli(args, env=None):
    """Run the CLI in a fresh interpreter, so import cost is measurable."""
    e = {**os.environ, "PYTHONPATH": SRC, **(env or {})}
    return subprocess.run([sys.executable, "-m", "retrace.cli", *args],
                          capture_output=True, text=True, env=e)


# ------------------------------------------------------- the lazy-import contract

def test_cli_half_does_not_import_textual():
    """Importing Textual costs 300-500ms; `retrace s foo` may not pay it."""
    code = (
        "import sys;"
        "from retrace import cli, query, indexer, sessions, actions, storage, render;"
        "print([m for m in sys.modules if m.split('.')[0] in "
        "('textual','rich','markdown_it','pygments')])"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         env={**os.environ, "PYTHONPATH": SRC})
    assert out.stdout.strip() == "[]", out.stdout


def test_search_stays_fast(indexed, env):
    """Guard on wall-clock for the whole command, cold interpreter included."""
    import time
    t0 = time.perf_counter()
    out = run_cli(["s", "ipsec"], env={"RETRACE_DB": str(env["db"])})
    elapsed = time.perf_counter() - t0
    assert out.returncode == 0, out.stderr
    assert "ipsec" in out.stdout
    assert elapsed < 0.6, f"{elapsed*1000:.0f}ms - something heavy got imported"


def test_textual_is_pinned_exactly():
    text = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
    assert 'dependencies = ["textual==' in text, \
        "Textual must be pinned exactly: suspend()/restore has regressed across " \
        "minor releases and that wedges the terminal"


# ------------------------------------------------------------------- subcommands

def test_bare_invocation_is_the_browser(monkeypatch):
    called = []
    monkeypatch.setattr(cli, "cmd_browse", lambda args: called.append(args.query))
    cli.main([])
    assert called == [""]


def test_unknown_flag_is_an_argparse_error():
    out = run_cli(["--nope"])
    assert out.returncode == 2


def test_version():
    out = run_cli(["--version"])
    assert out.returncode == 0 and out.stdout.startswith("retrace ")


def test_index_reports_counts(env, capsys):
    from conftest import claude_msg, jsonl
    jsonl(env["claude"] / "-a" / "a.jsonl", [claude_msg("user", "hello", session="cli")])
    cli.main(["index"])
    out = capsys.readouterr().out
    assert "scanned 1 transcripts, +1 messages, 1 indexed" in out


def test_index_verbose_lists_skips(env, capsys, monkeypatch):
    monkeypatch.setenv("RETRACE_VERBOSE", "1")
    p = env["claude"] / "-a" / "a.jsonl"
    p.parent.mkdir(parents=True)
    p.write_text("{bad json\n")
    cli.main(["index"])
    err = capsys.readouterr().err
    assert "skipped 1 unparseable entries" in err and "invalid JSON" in err


def test_search_sessions_mode(indexed, capsys):
    cli.main(["search", "c", "--sessions"])
    out = capsys.readouterr().out
    assert "hits" in out and "sessions," in out


def test_search_bad_query_exits_1(indexed, capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["s", 'NEAR("unclosed'])
    assert e.value.code == 1
    assert "bad FTS query" in capsys.readouterr().err


def test_sessions_and_projects(indexed, capsys):
    cli.main(["sessions"])
    out = capsys.readouterr().out
    assert "mikrotik" in out and "codex-real-id" in out
    cli.main(["projects"])
    out = capsys.readouterr().out
    assert "/home/max/projects/api" in out and "1 sessions" in out


def test_sessions_empty_index_exits(env, capsys):
    with pytest.raises(SystemExit):
        cli.main(["sessions"])
    assert "run: retrace index" in capsys.readouterr().err


def test_show_writes_to_stdout_when_not_a_tty(indexed, capsys):
    cli.main(["show", "codex-real-id"])
    out = capsys.readouterr().out
    assert "keyset cursor" in out


def test_name_set_print_clear(indexed, capsys):
    cli.main(["name", "claude-sess-1", "ipsec", "debugging"])
    assert "-> ipsec debugging" in capsys.readouterr().out
    cli.main(["name", "claude-sess-1"])
    assert capsys.readouterr().out.strip() == "ipsec debugging"
    cli.main(["name", "claude-sess-1", "--clear"])
    assert "cleared label" in capsys.readouterr().out
    cli.main(["name", "claude-sess-1"])
    assert capsys.readouterr().out.strip() == "(no label)"


def test_rm_index_only_then_restore(indexed, capsys):
    path = sessions.session_paths(indexed, "claude-sess-1")[0]
    cli.main(["rm", "claude-sess-1", "--index-only", "-y"])
    assert "transcript left on disk" in capsys.readouterr().out
    assert os.path.exists(path)

    cli.main(["excluded"])
    assert path in capsys.readouterr().out

    cli.main(["restore", "claude-sess-1"])
    assert "re-indexed" in capsys.readouterr().out
    db = __import__("retrace.storage", fromlist=["x"]).connect()
    assert db.execute("SELECT count(*) FROM msgs WHERE session='claude-sess-1'"
                      ).fetchone()[0] == 3


def test_restore_nothing_matching(indexed, capsys):
    with pytest.raises(SystemExit):
        cli.main(["restore", "whatever"])
    assert "nothing excluded matches" in capsys.readouterr().err


def test_excluded_when_empty(indexed, capsys):
    cli.main(["excluded"])
    assert capsys.readouterr().out.strip() == "nothing excluded"


def test_summarize_label(indexed, stubs, capsys):
    stubs.install("claude", "sys.stdout.write('A summary of the ipsec work.')")
    cli.main(["sum", "claude-sess-1", "--label"])
    assert "A summary of the ipsec work." in capsys.readouterr().out
    assert sessions.get_label(indexed, "claude-sess-1") == "A summary of the ipsec work."


def test_summarize_failure_exits_1(indexed, stubs, capsys, monkeypatch):
    monkeypatch.setenv("RETRACE_CLAUDE_SUMMARIZE", "no-such-cli {prompt}")
    with pytest.raises(SystemExit) as e:
        cli.main(["sum", "claude-sess-1"])
    assert e.value.code == 1
    assert "not on PATH" in capsys.readouterr().err


def test_stats(indexed, capsys):
    cli.main(["stats"])
    out = capsys.readouterr().out
    assert "claude" in out and "codex" in out and "labels:" in out


def test_no_index_flag_skips_the_freshness_pass(indexed, env, capsys):
    from conftest import claude_msg, jsonl
    jsonl(env["claude"] / "-new" / "n.jsonl",
          [claude_msg("user", "brand new session", session="fresh")])
    cli.main(["s", "brand", "--no-index"])
    assert "0 hits" in capsys.readouterr().out
    cli.main(["s", "brand"])
    assert "brand new session" in capsys.readouterr().out


def test_retrace_no_auto_env(indexed, env, monkeypatch, capsys):
    from conftest import claude_msg, jsonl
    monkeypatch.setenv("RETRACE_NO_AUTO", "1")
    jsonl(env["claude"] / "-new2" / "n.jsonl",
          [claude_msg("user", "also new", session="fresh2")])
    cli.main(["s", "also"])
    assert "0 hits" in capsys.readouterr().out


def test_broken_pipe_is_not_an_error(indexed, env):
    out = subprocess.run(
        f"{sys.executable} -m retrace.cli sessions -n 0 | head -1",
        shell=True, capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": SRC, "RETRACE_DB": str(env["db"])})
    assert out.returncode == 0, out.stderr
    assert "Traceback" not in out.stderr


def test_browse_refuses_an_empty_index(env, capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["browse"])
    assert e.value.code == 1
    assert "index is empty" in capsys.readouterr().err


# ------------------------------------------------- filters on single-session cmds

def test_rm_honours_source(indexed, capsys):
    """--source is accepted, so it has to mean something: this must delete the
    newest *codex* session, not the newest session of either kind."""
    from retrace import sessions as S
    claude_path = S.session_paths(indexed, "claude-sess-1")[0]
    codex_path = S.session_paths(indexed, "codex-real-id")[0]
    assert S.resolve_session(indexed, "1")[0]["session"] == "codex-real-id"

    cli.main(["rm", "1", "--source", "claude", "-y"])
    assert not os.path.exists(claude_path)
    assert os.path.exists(codex_path)


def test_rm_honours_project(indexed):
    from retrace import sessions as S
    target = S.session_paths(indexed, "claude-sess-2")[0]
    cli.main(["rm", "claude-sess", "--project", "gw", "-y"])
    assert not os.path.exists(target)
    assert os.path.exists(S.session_paths(indexed, "claude-sess-1")[0])


def test_show_honours_source(indexed, capsys):
    cli.main(["show", "1", "--source", "claude"])
    assert "mikrotik" in capsys.readouterr().out


def test_name_honours_source(indexed):
    # label words first: argparse before 3.12 cannot resume a trailing nargs="*"
    # positional after an option, so `name 1 --source claude some words` is a
    # usage error there and works on 3.12+. Documented in the README.
    cli.main(["name", "1", "labelled the claude one", "--source", "claude"])
    assert sessions.get_label(indexed, "claude-sess-1") == "labelled the claude one"
    assert sessions.get_label(indexed, "codex-real-id") is None


def test_a_filter_that_excludes_everything_says_so(indexed, capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["show", "codex-real-id", "--source", "claude"])
    assert e.value.code == 1
    err = capsys.readouterr().err
    assert "--source claude" in err


def test_index_reports_the_real_skipped_total(env, capsys, monkeypatch):
    monkeypatch.setenv("RETRACE_VERBOSE", "1")
    p = env["claude"] / "-a" / "a.jsonl"
    p.parent.mkdir(parents=True)
    p.write_text("{bad\n" * 700)
    cli.main(["index"])
    err = capsys.readouterr().err
    assert "skipped 700 unparseable entries" in err
    assert "200 more, not kept as examples" in err


def test_index_announces_a_tools_rebuild(env, capsys):
    from conftest import claude_msg, jsonl
    jsonl(env["claude"] / "-a" / "a.jsonl", [claude_msg("user", "hi", session="t")])
    cli.main(["index"])
    capsys.readouterr()
    cli.main(["index", "--include-tools"])
    assert "index was rebuilt" in capsys.readouterr().err


def test_search_finds_a_session_by_name(indexed, capsys):
    """A name is not message text, so FTS5 cannot match it."""
    sessions.set_label(indexed, "codex-real-id", "pagination-rework")
    cli.main(["s", "pagination-rework"])
    out = capsys.readouterr().out
    assert "1 session(s) named like that" in out
    assert "pagination-rework" in out
    assert "0 hits" in out


def test_named_sessions_are_shown_alongside_text_hits(indexed, capsys):
    """The failure this replaces: quoting a session's name anywhere gives the text
    search hits, and a name-only fallback then stays quiet about the session that
    is actually called that."""
    sessions.set_label(indexed, "codex-real-id", "conntrack")
    cli.main(["s", "conntrack"])
    out = capsys.readouterr().out
    assert "1 session(s) named like that" in out          # the session
    assert "haproxy.cfg" in out or "conntrack" in out     # and the messages
    assert "1 hits" in out


def test_search_matching_nothing_at_all_says_so(indexed, capsys):
    cli.main(["s", "zzzznotanywhere"])
    assert "nothing matched" in capsys.readouterr().err
