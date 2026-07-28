"""Environment-derived configuration.

Every value is read through a function rather than bound at import time.
Caching the database path in a module global makes the whole tree untestable
without reimporting it, and lets a child process inherit a stale index location.
So nothing here caches.
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_PREFIX = "RETRACE_"

#: per-message cap on indexed text; keeps the db lean and snippets sane
MAX_CHARS = 24_000

#: how much of a transcript the prescans read looking for cwd / id / name
PRESCAN_BYTES = 400_000

DEFAULT_SUMMARY_PROMPT = (
    "Below is a transcript of a past coding session. Summarise what was actually "
    "discussed and decided: the problem, the approach taken, the key decisions and "
    "why, what was changed, and anything left unresolved. Be concrete and concise. "
    "Do not offer to help further."
)

DEFAULT_RESUME = {
    "claude": "claude --resume {id}",
    "codex": "codex resume {id}",
}

# Both use documented non-interactive modes and read the transcript on stdin, so
# nothing depends on the directory the session was started in - unlike --resume,
# whose session lookup is scoped to the original project directory.
#
#   --no-session-persistence / --ephemeral : summarising must not create new
#       sessions, or retrace would index its own summaries on the next pass.
#   --skip-git-repo-check : without it `codex exec` refuses to run outside a
#       trusted directory.
#   -s read-only : summarising must never touch the filesystem.
DEFAULT_SUMMARIZE = {
    "claude": "claude -p --no-session-persistence {prompt}",
    "codex": "codex exec --ephemeral --skip-git-repo-check -s read-only {prompt}",
}


def env(name: str, default: str | None = None) -> str | None:
    """Read ``RETRACE_<name>``."""
    return os.environ.get(ENV_PREFIX + name, default)


def flag(name: str) -> bool:
    v = env(name)
    return bool(v) and v not in ("0", "", "false", "no")


def _int(name: str, default: int, minimum: int = 1) -> int:
    """A positive int, or the default.

    Nonsense in the environment must not become an exception three layers down:
    ``RETRACE_SUMMARY_TIMEOUT=-1`` would reach ``subprocess.run(timeout=-1)``, and
    ``RETRACE_SUMMARY_MAX=0`` would clamp a transcript to nothing.
    """
    try:
        value = int(env(name) or default)
    except ValueError:
        return default
    return value if value >= minimum else default


def db_path() -> Path:
    return Path(env("DB") or Path.home() / ".cache" / "retrace" / "index.db")


def roots() -> list[tuple[str, Path]]:
    """``[(source, root)]`` in a stable order."""
    return [
        ("claude", Path(env("CLAUDE_ROOT") or Path.home() / ".claude" / "projects")),
        ("codex", Path(env("CODEX_ROOT") or Path.home() / ".codex" / "sessions")),
    ]


def codex_index_path() -> Path:
    """Codex's own name index: ``{"id", "thread_name", "updated_at"}`` per line.

    Sits beside the sessions directory rather than inside it, which is why it is
    derived from the root instead of discovered by the transcript walk.
    """
    override = env("CODEX_INDEX")
    if override:
        return Path(override)
    return dict(roots())["codex"].parent / "session_index.jsonl"


def no_auto_index() -> bool:
    return flag("NO_AUTO")


def verbose() -> bool:
    return flag("VERBOSE")


def resume_template(source: str) -> str | None:
    tpl = env(f"{source.upper()}_RESUME")
    return tpl or DEFAULT_RESUME.get(source)


def summarize_template(source: str) -> str | None:
    tpl = env(f"{source.upper()}_SUMMARIZE")
    return tpl or DEFAULT_SUMMARIZE.get(source)


def summary_prompt() -> str:
    return env("SUMMARY_PROMPT") or DEFAULT_SUMMARY_PROMPT


def summary_max() -> int:
    return _int("SUMMARY_MAX", 400_000)


def summary_timeout() -> int:
    return _int("SUMMARY_TIMEOUT", 600)


def summary_cwd() -> str | None:
    return env("SUMMARY_CWD") or None


def pager_spec() -> str:
    return env("PAGER") or os.environ.get("PAGER") or "less"
