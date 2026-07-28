"""Things that leave the process: resume, summarise, page.

No ``/dev/tty`` anywhere: Textual owns the terminal and hands it back with
``App.suspend()``, so a child process just inherits stdio. Opening the terminal
by hand is only necessary when something else is holding our stdout, which is not
the case here.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from . import config, parsers
from .sessions import render_session


class ActionError(Exception):
    """Something a user needs to be told about, not a bug."""


# --------------------------------------------------------------------- resuming

def resume_argv(r: dict) -> list[str]:
    tpl = config.resume_template(r["source"])
    if not tpl:
        raise ActionError(f"no resume template for source {r['source']!r}")
    # split first, then format: {id} stays a single argv element regardless of
    # what it contains.
    return [a.format(id=r["session"], path=r.get("path") or "") for a in shlex.split(tpl)]


def resume_cwd(r: dict) -> str | None:
    """Where to chdir before handing the session back.

    Claude Code scopes session-id lookup to the current directory's slug, so this
    has to be the directory the session *started* in - not the last cwd it
    recorded, which is what the project label shows. A session that ran `cd`
    otherwise resolves to a directory whose slug holds no transcript, and the CLI
    reports `No conversation found with session ID`.

    Order: the recorded cwd that matches the transcript's own directory, then the
    un-slugged directory name, then the project label. Each is only used if it
    actually exists.
    """
    path = r.get("path")
    candidates = []
    if path:
        p = Path(path)
        candidates.append(parsers.origin_cwd(r.get("source", ""), p))
        candidates.append(parsers.unslug(r.get("source", ""), p) or None)
    candidates.append(r.get("project"))
    for c in candidates:
        if c and os.path.isdir(c):
            return c
    return None


def resume_display(r: dict) -> str:
    argv = resume_argv(r)
    cwd = resume_cwd(r)
    shown = " ".join(shlex.quote(a) for a in argv)
    return f"cd {shlex.quote(cwd)} && {shown}" if cwd else shown


def do_resume(r: dict, dry_run: bool = False) -> None:
    """Replace this process with the CLI's resume command.

    The chdir is required, not cosmetic: Claude Code scopes session-id lookup to
    the current project directory and its git worktrees, and otherwise reports
    ``No conversation found with session ID``.
    """
    argv = resume_argv(r)
    cwd = resume_cwd(r)
    if dry_run:
        return
    if cwd:
        os.chdir(cwd)
    try:
        os.execvp(argv[0], argv)
    except FileNotFoundError as e:
        raise ActionError(
            f"{argv[0]} not on PATH - override with "
            f"RETRACE_{r['source'].upper()}_RESUME='...{{id}}...'"
        ) from e


# ------------------------------------------------------------------ summarising

def clamp_transcript(text: str, limit: int | None = None):
    """Keep head and tail when a session is too big to send whole: the opening
    frames the problem, the end holds the conclusions. Returns
    ``(text, clipped)``."""
    limit = limit or config.summary_max()
    if len(text) <= limit:
        return text, False
    head, tail = int(limit * 0.55), int(limit * 0.45)
    elided = len(text) - head - tail
    return (
        text[:head]
        + f"\n\n[... {elided} characters elided from the middle ...]\n\n"
        + text[-tail:]
    ), True


def summarize_argv(source: str, prompt: str | None = None) -> list[str]:
    tpl = config.summarize_template(source)
    if not tpl:
        raise ActionError(f"no summarize template for {source!r}")
    return [a.format(prompt=prompt or config.summary_prompt())
            for a in shlex.split(tpl)]


def summarize_session(db, r: dict, which: str = "auto", prompt: str | None = None,
                      on_progress=None) -> str:
    """Render the session plain and pipe it to a CLI's headless mode.

    Directory-independent, unlike resume: the transcript arrives on stdin rather
    than being looked up by id.
    """
    source = r["source"] if which in (None, "auto") else which
    argv = summarize_argv(source, prompt)
    if not shutil.which(argv[0]):
        raise ActionError(
            f"{argv[0]} not on PATH - override with "
            f"RETRACE_{source.upper()}_SUMMARIZE='...{{prompt}}...'"
        )

    text, _jump = render_session(db, r, plain=True)   # never send ANSI to a model
    text, clipped = clamp_transcript(text)
    if on_progress:
        on_progress(
            f"{argv[0]}: {len(text)} chars{' (clipped)' if clipped else ''}, "
            f"{r['n']} messages - this takes a moment"
        )
    timeout = config.summary_timeout()
    try:
        proc = subprocess.run(
            argv, input=text, capture_output=True, text=True,
            timeout=timeout, cwd=config.summary_cwd(),
        )
    except subprocess.TimeoutExpired as e:
        raise ActionError(
            f"timed out after {timeout}s (RETRACE_SUMMARY_TIMEOUT)") from e
    except (OSError, ValueError) as e:
        # ValueError: a timeout subprocess refuses to accept. config._int keeps
        # that out, and this keeps it from ever being a traceback if it slips.
        raise ActionError(f"could not run {argv[0]}: {e}") from e
    if proc.returncode != 0:
        raise ActionError(
            f"{argv[0]} exited {proc.returncode}: {(proc.stderr or '').strip()[:400]}")
    out = (proc.stdout or "").strip()
    if not out:
        raise ActionError(f"{argv[0]} produced no output")
    return out


# ----------------------------------------------------------------------- paging

def pager_argv(jump: int = 1) -> list[str] | None:
    parts = shlex.split(config.pager_spec())
    if not parts or not shutil.which(parts[0]):
        return None
    if os.path.basename(parts[0]) == "less":
        parts += ["-R", f"+{jump}"]      # -R/+N are less-specific, not universal
    return parts


def page(text: str, jump: int = 1, fallback=None) -> None:
    """Show text in ``$PAGER``. Falls back to writing it out.

    Callers inside the TUI must wrap this in ``with app.suspend():``.
    """
    argv = pager_argv(jump)
    if argv is None:
        (fallback or sys.stdout.write)(text)
        return
    fd, tmp = tempfile.mkstemp(suffix=".retrace.txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        try:
            subprocess.run(argv + [tmp])
        except OSError as e:
            raise ActionError(f"could not run {argv[0]}: {e}") from e
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
