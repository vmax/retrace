"""argparse wiring for the non-interactive half.

Nothing in this module (or anything it imports) may import textual. `retrace s
foo` is a ~100ms command and importing Textual would add 300-500ms to it. The
interactive command imports the TUI inside its own function body; see
`cmd_browse`. tests/test_cli.py asserts that `sys.modules` stays clean.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys

from . import __version__, actions, config, indexer, query, render, sessions, storage


def _die(msg: str, code: int = 1):
    print(msg, file=sys.stderr)
    raise SystemExit(code)


def _open(args) -> sqlite3.Connection:
    db = storage.connect()
    if not getattr(args, "no_index", False):
        indexer.auto_index(db)
    return db


def _resolve(db, ref, pal, args=None) -> dict:
    """Resolve a session reference, honouring --source/--project.

    Passing them on is not cosmetic. `retrace rm 1 --source codex` has to mean
    the newest codex session; silently ignoring the filter would delete the newest
    session of either kind.
    """
    filters = {}
    if args is not None:
        filters = {"source": getattr(args, "source", None),
                   "project": getattr(args, "project", None)}
    try:
        return sessions.one_session(db, ref, **filters)
    except sessions.NotFound as e:
        _die(f"{e}{_scoped(args) if args is not None else ''} - "
             f"try: retrace sessions")
    except sessions.Ambiguous as e:
        # Ambiguity is never guessed at: list the candidates and exit 1.
        print(f"{len(e.rows)} sessions match {e.ref!r}, be more specific:\n",
              file=sys.stderr)
        print(render.session_list(e.rows, pal), file=sys.stderr)
        raise SystemExit(1)


def _scoped(args) -> str:
    """How a failed lookup should describe the filters that were applied."""
    bits = [f"--{k} {getattr(args, k)}" for k in ("source", "project")
            if getattr(args, k, None)]
    return f" (with {' '.join(bits)})" if bits else ""


# ----------------------------------------------------------------------- index

def cmd_index(args):
    db = storage.connect()
    run = indexer.full_reindex if args.full else indexer.index
    rep = run(db, include_tools=args.include_tools)
    for source, root in rep.missing_roots:
        print(f"  skip {source}: {root} not found", file=sys.stderr)
    total = db.execute("SELECT count(*) FROM msgs").fetchone()[0]
    if rep.rebuilt:
        print("--include-tools changed, so the index was rebuilt", file=sys.stderr)
    print(f"scanned {rep.scanned} transcripts, +{rep.added} messages, "
          f"{total} indexed -> {config.db_path()}")
    if rep.named:
        print(f"  {rep.named} session name(s) picked up from the CLI",
              file=sys.stderr)
    if rep.vanished:
        print(f"  {rep.vanished} transcript(s) disappeared while indexing",
              file=sys.stderr)
    if rep.skipped_total:
        print(f"skipped {rep.skipped_total} unparseable entries "
              f"(RETRACE_VERBOSE=1 to list)", file=sys.stderr)
        if config.verbose():
            for s in rep.skipped[:40]:
                print("  " + s, file=sys.stderr)
            if rep.skipped_total > len(rep.skipped):
                print(f"  ... {rep.skipped_total - len(rep.skipped)} more, "
                      f"not kept as examples", file=sys.stderr)


# ---------------------------------------------------------------------- search

def cmd_search(args):
    pal = render.palette(args.no_color)
    db = _open(args)
    try:
        rows = query.search(
            db, args.query, limit=args.limit, exact=args.exact, sort=args.sort,
            role=args.role, source=args.source, project=args.project,
            session=args.session, since=args.since, until=args.until,
        )
    except query.BadQuery as e:
        _die(f"bad FTS query: {e}\n"
             "quote literals containing punctuation: retrace s '\"foo-bar\"'")

    # Before the text hits, not instead of them: a session whose *name* matches is
    # the strongest match there is, and FTS5 cannot see names at all.
    named = query.sessions_named(db, args.query, source=args.source,
                                 project=args.project, limit=10)
    if named and not args.sessions:
        print(f"{pal.bold}{len(named)} session(s) named like that:{pal.off}")
        print(render.session_list(named, pal))
        print()

    if args.sessions:
        agg: dict[str, dict] = {}
        for r in rows:
            a = agg.setdefault(r["session"], {"n": 0, "score": 0.0, "source": r["source"],
                                              "project": r["project"], "ts": r["ts"],
                                              "path": r["path"]})
            a["n"] += 1
            a["score"] += r["score"]
            a["ts"] = min(a["ts"] or "", r["ts"] or "") or a["ts"]
        for sess, a in sorted(agg.items(), key=lambda kv: kv[1]["score"]):
            print(f"{pal.bold}{a['source']:6} {sess}{pal.off}  {a['n']} hits  "
                  f"{pal.dim}{(a['ts'] or '')[:19]}  {a['project']}{pal.off}")
            print(f"       {pal.dim}{a['path']}{pal.off}")
        print(f"\n{pal.dim}{len(agg)} sessions, {len(rows)} messages{pal.off}")
        return

    if rows:
        print(render.hits(rows, pal))
    print(f"{pal.dim}{len(rows)} hits{pal.off}")

    if not rows and not named:
        print(f"{pal.dim}nothing matched, by message text or by session name"
              f"{pal.off}", file=sys.stderr)


# -------------------------------------------------------------------- listings

def cmd_sessions(args):
    pal = render.palette(args.no_color)
    db = _open(args)
    rows = query.session_rows(db, limit=args.limit or None, project=args.project,
                              source=args.source, sort=args.sort)
    if not rows:
        _die("no sessions indexed yet - run: retrace index")
    print(render.session_list(rows, pal))
    if sys.stdout.isatty():
        print(f"\n{pal.dim}resume with: retrace resume <n>   "
              f"or browse: retrace{pal.off}")


def cmd_projects(args):
    pal = render.palette(args.no_color)
    db = _open(args)
    rows = query.project_rows(db, source=args.source, sort=args.sort,
                              limit=args.limit or None)
    if not rows:
        _die("no projects indexed yet - run: retrace index")
    for i, r in enumerate(rows, 1):
        print(f"{i:>3}  {pal.bold}{r['project']}{pal.off}")
        print(f"     {pal.dim}{(r['last'] or '')[:16]}  {r['sessions']:>4} sessions  "
              f"{r['messages']:>6} messages  "
              f"{r['sources'].replace(',', '+')}{pal.off}")
    if sys.stdout.isatty():
        print(f"\n{pal.dim}{len(rows)} folders - drill in: "
              f"retrace sessions --project <substring>{pal.off}")


# ------------------------------------------------------------- single sessions

def cmd_show(args):
    pal = render.palette(args.no_color)
    db = _open(args)
    r = _resolve(db, args.session, pal, args)
    text, _ = sessions.render_session(db, r, palette=pal.as_tuple())
    if sys.stdout.isatty() and not args.no_pager:
        actions.page(text)
    else:
        sys.stdout.write(text)


def cmd_resume(args):
    pal = render.palette(args.no_color)
    db = _open(args)
    r = _resolve(db, args.session, pal, args)
    try:
        print(f"{pal.dim}$ {actions.resume_display(r)}{pal.off}", file=sys.stderr)
        actions.do_resume(r, args.dry_run)
    except actions.ActionError as e:
        _die(str(e))


def cmd_name(args):
    pal = render.palette(args.no_color)
    db = _open(args)
    r = _resolve(db, args.session, pal, args)
    if args.clear:
        sessions.clear_label(db, r["session"])
        print(f"cleared label on {r['session']}")
        return
    if not args.name:
        print(r["label"] or "(no label)")
        return
    name = " ".join(args.name).strip()
    sessions.set_label(db, r["session"], name)
    print(f"{r['session']} -> {name}")


def cmd_rm(args):
    pal = render.palette(args.no_color)
    db = _open(args)
    r = _resolve(db, args.session, pal, args)
    paths = sessions.session_paths(db, r["session"])
    purge = not args.index_only

    if purge:
        print(f"{pal.bold}This deletes the transcript from disk. "
              f"It is the only copy.{pal.off}", file=sys.stderr)
    print(f"  {r['title'][:80]}\n  {r['source']}  {r['session']}  "
          f"{r['n']} messages", file=sys.stderr)
    for p in paths:
        print(f"  {p}", file=sys.stderr)

    if not args.yes:
        verb = "delete" if purge else "drop this from the index"
        # Fails closed: no tty means abort, never proceed (AGENTS.md rule 3).
        if not sys.stdin.isatty():
            _die("aborted: no tty to confirm on (use -y to skip confirmation)")
        try:
            if input(f"{verb}? [y/N] ").strip().lower() not in ("y", "yes"):
                _die("aborted")
        except (EOFError, KeyboardInterrupt):
            _die("aborted")

    n, gone, errors = sessions.delete_session(db, r, purge=purge)
    for e in errors:
        print(f"  could not remove {e}", file=sys.stderr)
    if purge:
        print(f"deleted {gone} transcript(s), {n} messages dropped from the index")
    else:
        print(f"dropped {n} messages from the index; transcript left on disk\n"
              f"{pal.dim}re-add with: retrace restore {r['session']}{pal.off}")


def cmd_restore(args):
    db = storage.connect()
    paths = sessions.restore(db, args.session)
    if not paths:
        _die("nothing excluded matches that - see: retrace excluded")
    for p in paths:
        print(f"un-excluded {p}")
    indexer.index(db, storage.meta_get(db, "include_tools", "0") == "1")
    print("re-indexed")


def cmd_excluded(args):
    db = storage.connect()
    rows = db.execute(
        "SELECT path, session, ts FROM excluded ORDER BY ts DESC").fetchall()
    if not rows:
        print("nothing excluded")
        return
    for path, sess, ts in rows:
        print(f"{(ts or '')[:19]}  {sess}\n  {path}")


def cmd_summarize(args):
    pal = render.palette(args.no_color)
    db = _open(args)
    r = _resolve(db, args.session, pal, args)
    try:
        summary = actions.summarize_session(
            db, r, which=args.with_, prompt=args.prompt,
            on_progress=lambda m: print(f"{pal.dim}{m}{pal.off}", file=sys.stderr),
        )
    except actions.ActionError as e:
        _die(str(e))
    print(summary)
    if args.label:
        import re
        first = re.sub(r"\s+", " ", summary).strip()[:120]
        sessions.set_label(db, r["session"], first)
        print(f"\n{pal.dim}labelled: {first}{pal.off}", file=sys.stderr)


def cmd_stats(args):
    db = storage.connect()
    p = config.db_path()
    print(f"db: {p} ({p.stat().st_size / 1e6:.1f} MB)" if p.exists() else "no index yet")
    for src, ns, nm, lo, hi in db.execute(
        "SELECT source, count(DISTINCT session), count(*), min(ts), max(ts) "
        "FROM msgs GROUP BY source"
    ):
        print(f"  {src:7} {ns:5} sessions  {nm:7} messages  "
              f"{(lo or '?')[:10]} .. {(hi or '?')[:10]}")
    nlab = db.execute("SELECT count(*) FROM labels").fetchone()[0]
    nexc = db.execute("SELECT count(*) FROM excluded").fetchone()[0]
    print(f"  labels: {nlab}   excluded: {nexc}   "
          f"tools indexed: {storage.meta_get(db, 'include_tools', '0') == '1'}")


# ------------------------------------------------------------------ interactive

def cmd_browse(args):
    """The TUI. Textual is imported here and nowhere else."""
    db = storage.connect()
    if not args.no_index:
        indexer.auto_index(db)
    if not db.execute("SELECT 1 FROM msgs LIMIT 1").fetchone():
        _die("index is empty - run: retrace index")
    from .tui.app import run_app
    raise SystemExit(run_app(db, args) or 0)


# --------------------------------------------------------------------- parser

def add_filters(p):
    p.add_argument("--source", choices=["claude", "codex"])
    p.add_argument("--project")
    p.add_argument("--no-index", action="store_true", help="skip the freshness pass")
    p.add_argument("--no-color", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="retrace",
        description="Search, browse and manage local Claude Code / Codex CLI sessions.",
        epilog="run `retrace` with no arguments for the interactive browser",
    )
    ap.add_argument("--version", action="version", version=f"retrace {__version__}")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("index", help="incrementally index transcripts")
    p.add_argument("--full", action="store_true", help="drop and rebuild")
    p.add_argument("--include-tools", action="store_true",
                   help="also index tool calls and results (bigger index, and it "
                        "will contain any secret a tool ever printed)")
    p.set_defaults(fn=cmd_index)

    for name in ("search", "s"):
        p = sub.add_parser(name, help="search and print hits")
        p.add_argument("query", help='FTS5 query: AND OR NOT NEAR() "phrase" prefix*')
        p.add_argument("-n", "--limit", type=int, default=20)
        p.add_argument("--role", choices=["user", "assistant"])
        p.add_argument("--session")
        p.add_argument("--since", help="ISO date, e.g. 2026-06-01")
        p.add_argument("--until")
        p.add_argument("--sessions", action="store_true", help="group hits by session")
        p.add_argument("--exact", action="store_true",
                       help="whole-token match; default treats each word as a prefix")
        p.add_argument("--sort", choices=["new", "old", "rank"], default="new")
        add_filters(p)
        p.set_defaults(fn=cmd_search)

    for name in ("browse", "i", "pick"):
        p = sub.add_parser(name, help="interactive browser (default with no args)")
        p.add_argument("query", nargs="?", default="")
        p.add_argument("--projects", action="store_true", help="open at folder level")
        p.add_argument("--role", choices=["all", "user", "assistant"], default="all")
        p.add_argument("--sort", choices=["new", "old"], default="new")
        p.add_argument("--dry-run", action="store_true",
                       help="print the resume command instead of running it")
        add_filters(p)
        p.set_defaults(fn=cmd_browse)

    p = sub.add_parser("sessions", help="list sessions with titles")
    p.add_argument("-n", "--limit", type=int, default=40, help="0 for all")
    p.add_argument("--sort", choices=["new", "old"], default="new")
    add_filters(p)
    p.set_defaults(fn=cmd_sessions)

    p = sub.add_parser("projects", aliases=["folders"], help="list project folders")
    p.add_argument("-n", "--limit", type=int, default=0, help="0 for all")
    p.add_argument("--sort", choices=["new", "old"], default="new")
    add_filters(p)
    p.set_defaults(fn=cmd_projects)

    p = sub.add_parser("show", help="read a session")
    p.add_argument("session", nargs="?", default="", help="default: most recent")
    p.add_argument("--no-pager", action="store_true")
    add_filters(p)
    p.set_defaults(fn=cmd_show)

    p = sub.add_parser("resume", help="hand the session back to claude/codex")
    p.add_argument("session", nargs="?", default="")
    p.add_argument("--dry-run", action="store_true")
    add_filters(p)
    p.set_defaults(fn=cmd_resume)

    p = sub.add_parser("name", help="label a session (retrace's own db; disk untouched)")
    p.add_argument("session")
    p.add_argument("name", nargs="*", help="omit to print the current label")
    p.add_argument("--clear", action="store_true")
    add_filters(p)
    p.set_defaults(fn=cmd_name)

    p = sub.add_parser("rm", help="delete a session's transcript and drop it from the index")
    p.add_argument("session")
    p.add_argument("--index-only", action="store_true",
                   help="keep the file, drop it from the index (undo: restore)")
    p.add_argument("-y", "--yes", action="store_true", help="skip the confirmation")
    add_filters(p)
    p.set_defaults(fn=cmd_rm)

    p = sub.add_parser("restore", help="undo an --index-only rm")
    p.add_argument("session")
    p.set_defaults(fn=cmd_restore)

    p = sub.add_parser("excluded", help="list sessions dropped from the index")
    p.set_defaults(fn=cmd_excluded)

    p = sub.add_parser("summarize", aliases=["sum"],
                       help="summarise a session with claude/codex")
    p.add_argument("session", nargs="?", default="")
    p.add_argument("--with", dest="with_", choices=["auto", "claude", "codex"],
                   default="auto")
    p.add_argument("--prompt")
    p.add_argument("--label", action="store_true",
                   help="also store the summary as the session's label")
    add_filters(p)
    p.set_defaults(fn=cmd_summarize)

    p = sub.add_parser("stats", help="index summary")
    p.set_defaults(fn=cmd_stats)

    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0].startswith("-") and argv[0] not in ("--version", "-h", "--help"):
        argv = ["browse"] + argv          # bare `retrace` opens the browser
    args = ap.parse_args(argv)
    if not getattr(args, "fn", None):
        ap.print_help()
        return 2
    try:
        args.fn(args)
    except BrokenPipeError:                # piped into head/less
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":               # pragma: no cover
    raise SystemExit(main())
