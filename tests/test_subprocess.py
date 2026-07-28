"""Subprocess plumbing, against stub binaries on PATH.

Neither `claude`, `codex` nor `less` needs to exist for any of this to run: the
stubs record argv, stdin and cwd, and can exit however we like.
"""

from __future__ import annotations

import os

import pytest

from retrace import actions, config, sessions


# ------------------------------------------------------------------ summarising

def test_summarize_sends_the_transcript_on_stdin(indexed, stubs):
    stubs.install("claude", "sys.stdout.write('THE SUMMARY')")
    r = sessions.session_by_id(indexed, "claude-sess-1")
    assert actions.summarize_session(indexed, r) == "THE SUMMARY"

    call = stubs.calls("claude")[0]
    assert call["argv"][1:] == ["-p", "--no-session-persistence",
                                config.summary_prompt()]
    assert "mikrotik ipsec" in call["stdin"]
    assert "claude-sess-1" in call["stdin"]


def test_summarize_picks_the_sessions_own_cli(indexed, stubs):
    stubs.install("codex", "sys.stdout.write('codex summary')")
    r = sessions.session_by_id(indexed, "codex-real-id")
    assert actions.summarize_session(indexed, r) == "codex summary"
    assert stubs.calls("codex")[0]["argv"][1] == "exec"


def test_summarize_with_override(indexed, stubs):
    stubs.install("codex", "sys.stdout.write('forced codex')")
    r = sessions.session_by_id(indexed, "claude-sess-1")
    assert actions.summarize_session(indexed, r, which="codex") == "forced codex"


def test_summarize_is_directory_independent(indexed, stubs, monkeypatch, tmp_path):
    """Unlike resume, the transcript arrives on stdin, so cwd is irrelevant."""
    other = tmp_path / "elsewhere"
    other.mkdir()
    monkeypatch.setenv("RETRACE_SUMMARY_CWD", str(other))
    stubs.install("claude", "sys.stdout.write('ok')")
    r = sessions.session_by_id(indexed, "claude-sess-1")
    actions.summarize_session(indexed, r)
    call = stubs.calls("claude")[0]
    assert os.path.realpath(call["cwd"]) == os.path.realpath(str(other))
    assert "mikrotik" in call["stdin"]


def test_summarize_nonzero_exit_is_reported(indexed, stubs):
    stubs.install("claude", "sys.stderr.write('boom happened'); sys.exit(3)")
    r = sessions.session_by_id(indexed, "claude-sess-1")
    with pytest.raises(actions.ActionError) as e:
        actions.summarize_session(indexed, r)
    assert "exited 3" in str(e.value) and "boom happened" in str(e.value)


def test_summarize_empty_output_is_reported(indexed, stubs):
    stubs.install("claude", "sys.exit(0)")
    r = sessions.session_by_id(indexed, "claude-sess-1")
    with pytest.raises(actions.ActionError, match="no output"):
        actions.summarize_session(indexed, r)


def test_summarize_timeout(indexed, stubs, monkeypatch):
    stubs.install("claude", "import time; time.sleep(30)")
    monkeypatch.setenv("RETRACE_SUMMARY_TIMEOUT", "1")
    r = sessions.session_by_id(indexed, "claude-sess-1")
    with pytest.raises(actions.ActionError, match="timed out after 1s"):
        actions.summarize_session(indexed, r)


def test_summarize_missing_binary(indexed, stubs, monkeypatch):
    monkeypatch.setenv("RETRACE_CLAUDE_SUMMARIZE", "no-such-cli-installed {prompt}")
    r = sessions.session_by_id(indexed, "claude-sess-1")
    with pytest.raises(actions.ActionError, match="not on PATH"):
        actions.summarize_session(indexed, r)


def test_summarize_template_override(indexed, stubs, monkeypatch):
    stubs.install("mysummarizer", "sys.stdout.write('custom')")
    monkeypatch.setenv("RETRACE_CLAUDE_SUMMARIZE",
                       "mysummarizer --flag 'a b' {prompt}")
    r = sessions.session_by_id(indexed, "claude-sess-1")
    assert actions.summarize_session(indexed, r) == "custom"
    argv = stubs.calls("mysummarizer")[0]["argv"]
    assert argv[1:3] == ["--flag", "a b"]        # shlex.split before .format


def test_summary_prompt_with_spaces_is_one_argv_element(indexed, stubs):
    stubs.install("claude", "sys.stdout.write('x')")
    r = sessions.session_by_id(indexed, "claude-sess-1")
    actions.summarize_session(indexed, r, prompt="two words here")
    assert stubs.calls("claude")[0]["argv"][-1] == "two words here"


def test_clamped_transcript_is_still_sent(indexed, stubs, monkeypatch):
    stubs.install("claude", "sys.stdout.write('x')")
    monkeypatch.setenv("RETRACE_SUMMARY_MAX", "200")
    r = sessions.session_by_id(indexed, "claude-sess-1")
    progress = []
    actions.summarize_session(indexed, r, on_progress=progress.append)
    assert progress and "clipped" in progress[0]
    assert len(stubs.calls("claude")[0]["stdin"]) <= 260


# ---------------------------------------------------------------------- resuming

def test_resume_argv_and_cwd(indexed):
    r = sessions.session_by_id(indexed, "claude-sess-1")
    assert actions.resume_argv(r) == ["claude", "--resume", "claude-sess-1"]
    assert actions.resume_cwd(r) is None          # /home/max/... does not exist here


def test_resume_chdirs_into_an_existing_project(indexed, tmp_path, db, env):
    from retrace import indexer
    proj = tmp_path / "realproject"
    proj.mkdir()
    from conftest import claude_msg, jsonl
    jsonl(env["claude"] / "-x" / "r.jsonl",
          [claude_msg("user", "in a real dir", session="realdir", cwd=str(proj))])
    indexer.index(db)
    r = sessions.session_by_id(db, "realdir")
    assert actions.resume_cwd(r) == str(proj)
    assert actions.resume_display(r).startswith(f"cd {proj} && claude --resume")


def test_resume_execs(indexed, stubs, tmp_path):
    """os.execvp replaces the process, so run it in a child."""
    import subprocess
    import sys
    stubs.install("claude", "sys.stdout.write('RESUMED ' + ' '.join(sys.argv[1:]))")
    code = (
        "import sys; sys.path.insert(0, %r);"
        "from retrace import storage, sessions, actions;"
        "db = storage.connect();"
        "actions.do_resume(sessions.session_by_id(db, 'claude-sess-1'))"
        % str(tmp_path.parents[100] if False else
              __import__("pathlib").Path(__file__).resolve().parents[1] / "src")
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         env={**os.environ})
    assert "RESUMED --resume claude-sess-1" in out.stdout, out.stderr


def test_resume_missing_binary_is_an_actionerror(indexed, monkeypatch):
    monkeypatch.setenv("RETRACE_CLAUDE_RESUME", "definitely-not-installed {id}")
    r = sessions.session_by_id(indexed, "claude-sess-1")
    with pytest.raises(actions.ActionError, match="not on PATH"):
        actions.do_resume(r)


def test_resume_dry_run_does_not_exec(indexed, monkeypatch):
    called = []
    monkeypatch.setattr("os.execvp", lambda *a: called.append(a))
    r = sessions.session_by_id(indexed, "claude-sess-1")
    actions.do_resume(r, dry_run=True)
    assert called == []


def test_resume_template_override(indexed, monkeypatch):
    monkeypatch.setenv("RETRACE_CODEX_RESUME", "codex --continue {id} --file {path}")
    r = sessions.session_by_id(indexed, "codex-real-id")
    argv = actions.resume_argv(r)
    assert argv[:3] == ["codex", "--continue", "codex-real-id"]
    assert argv[-1].endswith(".jsonl")


def test_no_template_for_unknown_source(indexed):
    with pytest.raises(actions.ActionError, match="no resume template"):
        actions.resume_argv({"source": "gemini", "session": "x"})


# ------------------------------------------------------------------------ pager

def test_pager_receives_a_file_and_the_text(stubs, monkeypatch, tmp_path):
    stubs.install("mypager", "print(open(sys.argv[-1]).read())")
    monkeypatch.setenv("RETRACE_PAGER", "mypager")
    actions.page("some rendered session\n", jump=5)
    call = stubs.calls("mypager")[0]
    assert call["argv"][-1].endswith(".retrace.txt")
    assert "-R" not in call["argv"]        # -R/+N are for less only


def test_pager_less_gets_jump_flags(monkeypatch, stubs):
    stubs.install("less", "sys.exit(0)")
    monkeypatch.setenv("RETRACE_PAGER", "less")
    actions.page("x", jump=42)
    assert stubs.calls("less")[0]["argv"][1:3] == ["-R", "+42"]


def test_pager_falls_back_when_absent(monkeypatch):
    monkeypatch.setenv("RETRACE_PAGER", "no-such-pager-anywhere")
    got = []
    actions.page("fallback text", fallback=got.append)
    assert got == ["fallback text"]


def test_pager_removes_its_tempfile(stubs, monkeypatch):
    stubs.install("mypager", "print(sys.argv[-1])")
    monkeypatch.setenv("RETRACE_PAGER", "mypager")
    actions.page("x")
    leftover = stubs.calls("mypager")[0]["argv"][-1]
    assert not os.path.exists(leftover)


def test_pager_nonzero_exit_is_not_fatal(stubs, monkeypatch):
    stubs.install("mypager", "sys.exit(1)")
    monkeypatch.setenv("RETRACE_PAGER", "mypager")
    actions.page("x")            # a user quitting the pager is not an error


# --------------------------------------------------- hostile environment values

@pytest.mark.parametrize("value", ["-1", "0", "not-a-number", ""])
def test_nonsense_timeout_falls_back_to_the_default(indexed, stubs, monkeypatch, value):
    """`subprocess.run(timeout=-1)` raises ValueError from deep inside a summary.
    Nonsense in the environment is not an exception the user should ever see."""
    monkeypatch.setenv("RETRACE_SUMMARY_TIMEOUT", value)
    assert config.summary_timeout() == 600
    stubs.install("claude", "sys.stdout.write('fine')")
    r = sessions.session_by_id(indexed, "claude-sess-1")
    assert actions.summarize_session(indexed, r) == "fine"


@pytest.mark.parametrize("value", ["-5", "0", "junk"])
def test_nonsense_summary_max_falls_back(monkeypatch, value):
    monkeypatch.setenv("RETRACE_SUMMARY_MAX", value)
    assert config.summary_max() == 400_000
    text, clipped = actions.clamp_transcript("still here")
    assert (text, clipped) == ("still here", False)


def test_a_timeout_value_subprocess_rejects_is_an_actionerror(indexed, stubs,
                                                              monkeypatch):
    stubs.install("claude", "sys.stdout.write('x')")
    monkeypatch.setattr(config, "summary_timeout", lambda: -1)
    r = sessions.session_by_id(indexed, "claude-sess-1")
    with pytest.raises(actions.ActionError):
        actions.summarize_session(indexed, r)
