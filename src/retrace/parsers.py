"""Tolerant transcript parsing.

Both Claude Code and Codex CLI document their transcript format as internal and
reshape it between releases. So nothing in this module asserts a layout: there
are no dataclasses, no TypedDicts and no strict deserialisation. Field
extraction looks for text-bearing *shapes*, every scalar bound for a DB column
goes through `as_text`, and an unrecognised shape yields no text rather than an
exception.

Observed in the wild, all of which used to crash a stricter parser:
``type`` as a dict (JSON Schema fragments nest it as an object, which is
unhashable and blows up set membership), ``role`` as a list, ``timestamp`` as a
dict.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .config import PRESCAN_BYTES

TEXT_KEYS = frozenset(
    {"text", "output_text", "input_text", "summary_text", "reasoning_text"}
)

TOOL_CALL_KEYS = frozenset(
    {"tool_use", "function_call", "custom_tool_call", "local_shell_call"}
)
TOOL_RESULT_KEYS = frozenset(
    {"tool_result", "function_call_output", "custom_tool_call_output"}
)

#: Keys `harvest` refuses to descend into.
#:
#: Tool declarations carry JSON Schema, which is where the dict-valued ``type``
#: comes from, and Codex writes its entire system prompt into ``instructions``
#: on every single session - indexing that would add hundreds of identical
#: copies of the same paragraph to the corpus.
SKIP_KEYS = frozenset(
    {
        "uuid", "parentUuid", "id", "call_id", "signature", "encrypted_content",
        "input_schema", "parameters", "properties", "tools", "instructions",
        "tool_choice", "schema", "$defs", "definitions",
    }
)

MAX_DEPTH = 12


def as_text(v, limit: int | None = 120) -> str | None:
    """Coerce anything a transcript can hold into a string (or None).

    Transcript fields are not type-stable across CLI versions; never let a dict
    or a list reach a TEXT column.
    """
    if v is None:
        return None
    if isinstance(v, str):
        return v[:limit] if limit else v
    if isinstance(v, (int, float, bool)):
        return str(v)
    try:
        s = json.dumps(v, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        s = str(v)
    return s[:limit] if limit else s


def _kind(node) -> str | None:
    """``node["type"]``, but only if it is a string.

    A JSON Schema fragment gives ``{"type": {"type": "string"}}``; a dict is
    unhashable, so a bare ``node.get("type") in SOME_SET`` raises TypeError.
    """
    t = node.get("type")
    return t if isinstance(t, str) else None


def harvest(node, out: list[str], include_tools: bool = False, depth: int = 0) -> None:
    """Append every text-bearing string reachable from ``node`` to ``out``."""
    if depth > MAX_DEPTH or node is None or isinstance(node, (str, int, float, bool)):
        return
    if isinstance(node, list):
        for v in node:
            harvest(v, out, include_tools, depth + 1)
        return
    if not isinstance(node, dict):
        return

    t = _kind(node)

    if t in TEXT_KEYS or t is None:
        if isinstance(node.get("text"), str):
            out.append(node["text"])
            return
    if t == "thinking" and isinstance(node.get("thinking"), str):
        out.append(node["thinking"])
        return
    if t in TOOL_CALL_KEYS:
        if include_tools:
            name = as_text(node.get("name") or node.get("tool_name") or "?")
            args = node.get("input", node.get("arguments", node.get("action", "")))
            if not isinstance(args, str):
                args = as_text(args, 4000) or ""
            out.append(f"[tool:{name}] {args[:4000]}")
        return
    if t in TOOL_RESULT_KEYS:
        if include_tools:
            body = node.get("content", node.get("output", ""))
            if not isinstance(body, str):
                body = as_text(body, 4000) or ""
            out.append(f"[result] {body[:4000]}")
        return

    for k, v in node.items():
        if k in SKIP_KEYS:
            continue
        harvest(v, out, include_tools, depth + 1)


def _entry(role, ts, text, session, cwd, key=None) -> dict:
    """A parsed line. Deliberately a plain dict: the shape of the input is not
    stable enough to justify a type, and callers only read these five fields."""
    return {
        "role": role or "?",
        "ts": ts,
        "text": text or "",
        "session": session,
        "cwd": cwd,
        "key": key,
    }


def parse_claude(obj: dict, fallback_session: str, include_tools: bool = False):
    kind = as_text(obj.get("type"), 40)
    sess = as_text(obj.get("sessionId"), 80) or fallback_session
    ts = as_text(obj.get("timestamp"), 40)
    cwd = as_text(obj.get("cwd"), 400)
    uid = as_text(obj.get("uuid"), 80)

    if kind == "summary":
        return _entry(
            "summary", ts, as_text(obj.get("summary"), None) or "", sess, cwd,
            uid or as_text(obj.get("leafUuid"), 80),
        )

    msg = obj.get("message")
    if not isinstance(msg, dict):
        return None
    role = as_text(msg.get("role"), 40) or kind or "?"
    if obj.get("isSidechain"):
        role = f"{role}/sub"          # subagent turns are still user/assistant turns

    parts: list[str] = []
    content = msg.get("content")
    if isinstance(content, str):
        parts.append(content)
    else:
        harvest(content, parts, include_tools)

    return _entry(role, ts, "\n".join(parts), sess, cwd, uid)


def parse_codex(obj: dict, fallback_session: str, include_tools: bool = False):
    payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else obj
    # The kind marker lives at the top level in some versions and inside payload
    # in others; payload wins when both are present. Reading only one of the two
    # silently broke session-id extraction, which broke `codex resume`.
    ptype = as_text(payload.get("type"), 60)
    otype = as_text(obj.get("type"), 60)
    kinds = {k for k in (ptype, otype) if k}
    ts = as_text(obj.get("timestamp"), 40) or as_text(payload.get("timestamp"), 40)
    cwd = as_text(payload.get("cwd"), 400)

    if kinds & {"session_meta", "turn_context"}:
        # Metadata only: no searchable body, but it carries the cwd and the real
        # session id - the one `codex resume` accepts, which is not the filename
        # stem. Tool schemas hang off here too and are deliberately not walked.
        return _entry(
            "meta", ts, "", as_text(payload.get("id"), 80) or fallback_session, cwd
        )
    if kinds & {"event_msg", "state"}:
        return None

    role = as_text(payload.get("role"), 40) or ptype or otype or "?"
    uid = as_text(payload.get("id") or obj.get("id"), 80)

    parts: list[str] = []
    content = payload.get("content")
    if isinstance(content, str):
        parts.append(content)
    elif content is not None:
        harvest(content, parts, include_tools)
    else:
        harvest(payload, parts, include_tools)

    return _entry(role, ts, "\n".join(parts), fallback_session, cwd, uid)


PARSERS = {"claude": parse_claude, "codex": parse_codex}


# ------------------------------------------------------------------- prescans

# Claude Code has real session names (`claude -n`, /rename, ctrl-R in its own
# picker). We read one if the transcript carries it, and never write it.
#
# Two places it has been seen. Some versions may put it in a field; current ones
# record the slash command in the conversation itself, as a user entry whose text
# contains:
#
#     <command-name>/rename</command-name>
#     <command-message>rename</command-message>
#     <command-args>the-new-name</command-args>
#
# That entry can be anywhere in the file - a rename usually happens once the
# session has a subject, i.e. late - so it is picked up per line while indexing
# rather than by a bounded prescan of the head. Last one wins: renames append.
NAME_RE = re.compile(
    rb'"(?:sessionName|session_name|customName)"\s*:\s*"((?:[^"\\]|\\.)*)"'
)
RENAME_RE = re.compile(
    r"<command-name>\s*/rename\s*</command-name>\s*(?:\\n|\s)*"
    r"<command-message>\s*rename\s*</command-message>\s*(?:\\n|\s)*"
    r"<command-args>\s*(.*?)\s*</command-args>",
    re.DOTALL,
)


def codex_names(path: Path) -> dict[str, str]:
    """Codex's session name index, ``{session id: name}``.

    Codex renames are not in the transcript at all - they live in a sibling
    ``session_index.jsonl``. Absent file, unreadable file, or a line in a shape we
    do not recognise all mean "no names", never an error.
    """
    out: dict[str, str] = {}
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(obj, dict):
                    continue
                sid = as_text(obj.get("id"), 80)
                name = as_text(obj.get("thread_name") or obj.get("name"), 200)
                if sid and name:
                    out[sid] = name          # later lines win
    except OSError:
        return out
    return out
CWD_RE = re.compile(rb'"cwd"\s*:\s*"((?:[^"\\]|\\.)*)"')
UUID_RE = re.compile(
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)

def codex_sid(blob: bytes, stem: str) -> str | None:
    """The id `codex resume` accepts: ``payload.id`` of the ``session_meta``
    line, else a UUID in the filename.

    A bare regex for ``"id"`` would happily match the first tool call's id, so
    this actually parses the opening lines - there are only ever a handful before
    session_meta appears, and a failure to parse just falls through.
    """
    for raw in blob.split(b"\n", 40)[:40]:
        if b"session_meta" not in raw:
            continue
        try:
            obj = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else obj
        sid = as_text(payload.get("id"), 80)
        if sid:
            return sid
    m = UUID_RE.search(stem)
    return m.group(1) if m else None


def _unjson(raw: bytes) -> str | None:
    try:
        v = json.loads(b'"' + raw + b'"')
    except ValueError:
        return None
    return v if isinstance(v, str) else None


def rename_from_text(text: str) -> str | None:
    """The name a ``/rename`` entry sets, if this is one.

    Requires the ``<command-message>rename</command-message>`` middle element, so
    a conversation that merely *quotes* the markup - which happens as soon as
    anyone works on tooling like this - is not mistaken for a rename.
    """
    if not text or "/rename" not in text:
        return None
    m = RENAME_RE.search(text)
    if not m:
        return None
    name = re.sub(r"\s+", " ", m.group(1)).strip()
    return name[:200] if name else None


def cwd_values(path: Path, nbytes: int | None = None) -> list[str]:
    """Every distinct ``cwd`` in a transcript, in the order it first appears."""
    try:
        with path.open("rb") as f:
            blob = f.read() if nbytes is None else f.read(nbytes)
    except OSError:
        return []
    out: list[str] = []
    for m in CWD_RE.finditer(blob):
        v = _unjson(m.group(1))
        if v and v not in out:
            out.append(v)
    return out


def origin_cwd(source: str, path: Path) -> str | None:
    """The directory a Claude Code session *started* in, or None.

    This is the one that matters for resuming, and it is not the same thing as
    the project label. A session can `cd` while it runs - each entry records the
    cwd at the time - but the transcript stays in the slugged directory of where
    it started. `claude --resume <id>` scopes its lookup to the slug of the
    current directory, so resuming from the last-seen cwd looks in a directory
    that does not contain the transcript and reports
    ``No conversation found with session ID``.

    Found by matching, not by un-slugging: the slug replaces every ``/`` with
    ``-`` and is therefore ambiguous for any directory whose name contains a
    hyphen. Matching a recorded cwd against the directory name has no such
    problem.
    """
    if source != "claude":
        return None
    want = path.parent.name
    for cwd in cwd_values(path):
        if cwd.replace("/", "-") == want:
            return cwd
    return None


def unslug(source: str, path: Path) -> str:
    """Reverse Claude Code's cwd slug. Lossy - a real ``cwd`` beats this.

    ``-home-max-fingular-infra`` reverses to ``/home/max/fingular/infra``, which
    is wrong whenever a directory name contains a hyphen.
    """
    if source != "claude":
        return ""
    d = path.parent.name
    return "/" + d.lstrip("-").replace("-", "/") if d.startswith("-") else d


def prescan(source: str, path: Path, nbytes: int = PRESCAN_BYTES) -> dict:
    """One read-only pass over the head of a transcript for its metadata.

    Returns ``{"project", "sid", "cli_name"}``. Read-only and bounded: this is
    regex over the first few hundred KB, not a parse of the whole file.
    """
    try:
        with path.open("rb") as f:
            blob = f.read(nbytes)
    except OSError:
        blob = b""

    m = CWD_RE.search(blob)
    project = (_unjson(m.group(1)) if m else None) or unslug(source, path)

    sid = codex_sid(blob, path.stem) if source == "codex" else None

    name = None
    for m in NAME_RE.finditer(blob):   # last match wins: renames append
        name = _unjson(m.group(1))
    if name is not None and (not name or len(name) >= 200):
        name = None

    return {"project": project, "sid": sid, "cli_name": name}
