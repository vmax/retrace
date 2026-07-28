#!/usr/bin/env python3
"""Regenerate docs/retrace.svg from a synthetic corpus.

    uv run --extra dev python docs/make_screenshot.py

Synthetic on purpose. A screenshot of this tool is a screenshot of whoever ran
it: the real thing is full of your own sessions, project paths and pasted files,
and none of that belongs in a repository. The corpus below is invented.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

SESSIONS = [
    ("/home/dev/infra", "2026-07-21", "claude", [
        ("user", "the ipsec tunnel to the branch office keeps dropping every "
                 "few hours, where do I even start looking"),
        ("assistant", "Start with the SA lifetimes on both ends - a rekey "
                      "mismatch shows up exactly like this. Then check conntrack "
                      "for the ESP flows."),
        ("user", "rekey interval is 8h on ours and 1h on theirs"),
        ("assistant", "That's it. The shorter side rekeys, the longer side keeps "
                      "using the old SA until traffic dies. Match them."),
    ]),
    ("/home/dev/infra", "2026-07-19", "claude", [
        ("user", "write an alerting rule for certificate expiry, 30 and 7 days"),
        ("assistant", "Two rules off the same probe metric, different `for` "
                      "durations and severities. Here's the promql."),
    ]),
    ("/home/dev/api", "2026-07-18", "codex", [
        ("user", "rewrite the pagination to a keyset cursor, the offset queries "
                 "are timing out on the large tenants"),
        ("assistant", "Cursor over (created_at, id) so it stays total. The index "
                      "you already have covers it; offset is what was scanning "
                      "the whole table."),
        ("user", "what happens to a cursor when a row is deleted"),
        ("assistant", "Nothing - it's a position, not a row reference. The next "
                      "page starts after that key whether the row is still there "
                      "or not."),
    ]),
    ("/home/dev/api", "2026-07-15", "codex", [
        ("user", "the migration locked the table for 40 seconds in staging"),
        ("assistant", "ADD COLUMN with a default rewrites the table on this "
                      "version. Add it nullable, backfill in batches, then set "
                      "the default."),
    ]),
    ("/home/dev/retrace", "2026-07-12", "claude", [
        ("user", "searching my own sessions is impossible, I know I debugged "
                 "this exact ipsec thing months ago"),
        ("assistant", "Index the transcripts into SQLite FTS5 and search that. "
                      "They're append-only JSONL, so incremental indexing is a "
                      "stat() per file."),
    ]),
    ("/home/dev/landing", "2026-07-08", "claude", [
        ("user", "why is the lighthouse score 61 on mobile"),
        ("assistant", "Two render-blocking fonts and an unsized hero image. The "
                      "layout shift is most of the penalty."),
    ]),
    ("/home/dev/warehouse", "2026-06-30", "codex", [
        ("user", "dedupe the events table, there are exact duplicates from the "
                 "replay we ran"),
        ("assistant", "row_number() over the natural key, delete where rn > 1, "
                      "then add the unique index so it cannot happen again."),
    ]),
]


def build(root: Path) -> None:
    claude = root / "claude" / "projects"
    codex = root / "codex" / "sessions"
    for i, (cwd, day, source, turns) in enumerate(SESSIONS):
        sid = f"{source[0]}{i:03d}f4e2-8a1b-4c7d-9e3f-{i:012d}"
        lines = []
        if source == "codex":
            lines.append({"timestamp": f"{day}T09:00:00Z", "type": "session_meta",
                          "payload": {"type": "session_meta", "id": sid, "cwd": cwd}})
        for n, (role, text) in enumerate(turns):
            ts = f"{day}T09:{n * 7 + 1:02d}:00Z"
            if source == "claude":
                lines.append({
                    "type": role, "sessionId": sid, "timestamp": ts, "cwd": cwd,
                    "uuid": f"{sid}-{n}",
                    "message": {"role": role, "content": [{"type": "text", "text": text}]},
                })
            else:
                key = "input_text" if role == "user" else "output_text"
                lines.append({
                    "timestamp": ts, "type": "response_item",
                    "payload": {"type": "message", "role": role,
                                "content": [{"type": key, "text": text}]},
                })
        if source == "claude":
            path = claude / cwd.replace("/", "-") / f"{sid}.jsonl"
        else:
            path = codex / day.replace("-", "/") / f"rollout-{day}T09-00-00-{sid}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(o) + "\n" for o in lines))

    os.environ["RETRACE_CLAUDE_ROOT"] = str(claude)
    os.environ["RETRACE_CODEX_ROOT"] = str(codex)
    os.environ["RETRACE_DB"] = str(root / "index.db")
    os.environ["RETRACE_NO_AUTO"] = "1"


async def shoot(out: Path, query: str = "") -> None:
    from retrace import indexer, query as q, sessions, storage
    from retrace.tui.app import RetraceApp

    db = storage.connect()
    indexer.index(db)
    newest_infra = next(r for r in q.session_rows(db, limit=None)
                        if r["project"].endswith("/infra"))
    sessions.set_label(db, newest_infra["session"],
                       "ipsec rekey mismatch, branch office")

    app = RetraceApp(db, query_text=query)
    async with app.run_test(size=(118, 30)) as pilot:
        await pilot.pause(0.3)
        await pilot.pause()
        app.save_screenshot(str(out))
    print(f"wrote {out} ({len(app.rows)} rows)")


#: A fixed, obviously-synthetic location rather than a temp dir: transcript paths
#: show up in the preview pane, and `/tmp/retrace-demo/...` reads as a demo while
#: `/var/folders/bc/08b1.../T/retrace-shot-9k2/...` just reads as noise.
DEMO = Path("/tmp/retrace-demo")


def main() -> int:
    shutil.rmtree(DEMO, ignore_errors=True)
    try:
        build(DEMO)
        import asyncio
        asyncio.run(shoot(ROOT / "docs" / "retrace.svg"))
        asyncio.run(shoot(ROOT / "docs" / "retrace-search.svg", query="cursor"))
    finally:
        shutil.rmtree(DEMO, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
