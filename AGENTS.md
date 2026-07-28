# AGENTS.md

Working notes for anyone — human or agent — changing this codebase. The README is
for users; this is the stuff you need before you touch the code.

`CLAUDE.md` is a symlink to this file.

## What this is

`retrace` indexes local Claude Code and Codex CLI session transcripts into SQLite
FTS5 and gives you a CLI plus a Textual browser over them. Single user, local
only, no daemon, no network of its own.

```
src/retrace/
  config.py    env lookups (RETRACE_*), roots, command templates
  storage.py   schema, connect(), meta k/v, the FTS5 delete protocol
  parsers.py   as_text, harvest, parse_claude, parse_codex, prescan
  indexer.py   incremental walk, offsets, exclusions, full rebuild
  query.py     prep_query, search, session_rows, project_rows, message_feed
  sessions.py  resolve_session, labels, delete_session, render_session
  actions.py   resume, summarize, pager
  render.py    ANSI output for the CLI
  cli.py       argparse; imports the TUI inside cmd_browse only
  tui/app.py   RetraceApp
  tui/screens.py  confirm / rename / text modals
```

## Rules that are not negotiable

Each of these exists because breaking it caused data loss, a crash, or a
terminal you had to `reset`. Every one has a named test; `tests/` is organised
around them.

1. **Never write to a transcript file.** Read and `unlink`, nothing else. Tests
   md5sum the corpus before and after a full index + browse + search cycle.
2. **FTS5 external-content deletes go through `storage.delete_messages`.** The
   index does not hold the text, so it must be told what is going away *before*
   the content row does:
   ```sql
   INSERT INTO msgs_fts(msgs_fts, rowid, text) VALUES('delete', ?, ?);
   DELETE FROM msgs WHERE ...;
   ```
   Skip it and queries return phantom rows. Note that bare
   `INSERT INTO msgs_fts(msgs_fts) VALUES('integrity-check')` does **not** catch
   this on SQLite 3.53 — it only checks the index against itself. The
   `rank=1` form compares index to content; `storage.fts_integrity_ok` runs both.
   No raw `DELETE FROM msgs` anywhere else — a test greps for it.
3. **Destructive actions confirm and fail closed.** No TTY means abort. `-y` is
   the only bypass. An ambiguous session reference lists candidates and exits 1;
   it is never guessed at.
4. **`labels`, `excluded` and `meta` survive `index --full`.** Only `msgs`,
   `msgs_fts` and `files` are rebuilt.
5. **Exclusions are honoured by incremental *and* full indexing,** or a session
   the user removed silently comes back.
6. **A partial trailing line is not consumed.** A final chunk with no newline is
   a half-written line from a live session: don't parse it, don't advance the
   stored offset past it.
7. **Row keys are stable** (`entry_key` = transcript uuid, else `path:line`), so
   re-ingesting a file is idempotent. A shrunk file is purged and re-read from 0.
8. **A filter a command accepts, a command uses.** `--source`/`--project` narrow
   session resolution, including ordinals: `rm 1 --source codex` deletes the
   newest *codex* session. An accepted-and-ignored filter on a destructive command
   deletes the wrong thing.
9. **Never over- or under-state what happened.** Counts reported to the user are
   real counts: the skipped-entry *examples* are capped at 500, the *total* is
   not, and a truncated list says how much it truncated.
10. **Only user/assistant text is indexed by default.** Tool results contain file
   reads and terminal output — every secret an agent ever printed. `--include-tools`
    is opt-in, and the choice is persisted so an automatic pass can't change it.
    Changing it **rebuilds**: an incremental pass only reads what grew, so
    flipping the flag would otherwise leave half the corpus indexed one way and
    half the other with no way to tell which.
11. **Never send ANSI to a model.** `render_session(plain=True)` for anything
    piped to a CLI.
12. **The CLI half must not import Textual.** `retrace s foo` is a ~90ms command;
    Textual costs 300–500ms to import. It is imported inside `cli.cmd_browse` and
    nowhere else, and a test asserts `sys.modules` stays clean.

## Indexing: the file is a moving target

A transcript is being appended to by another process, and the corpus is pruned
behind our back. Three consequences:

- **`(size, mtime)` is the fingerprint, not `size`.** A transcript can be
  rewritten to exactly the same length; size alone calls that untouched and leaves
  stale messages in the index. Same size with a different mtime restarts the file
  from offset 0 (after purging its rows), rather than trusting the stored offset.
- **Anything can disappear between `stat()` and `open()`.** A vanished file is
  counted (`Report.vanished`), its rows and `files` entry are dropped, and the
  pass continues. One pruned transcript must never end a freshness pass that runs
  before every query.
- **A shrunk, rewritten, or same-size-different-mtime file is purged first.**
  Re-reading from 0 on top of existing rows is how you get duplicates.

## Resuming: the origin directory, not the project label

`claude --resume <id>` scopes its lookup to the slug of the current directory. A
session that runs `cd` keeps its transcript in the slug directory of where it
**started**, while its later entries record the new cwd — and the project label
shows the latter. Resuming from the label therefore lands in a directory whose
slug holds no transcript and the CLI says `No conversation found with session ID`,
for a session retrace can read perfectly well.

`parsers.origin_cwd` finds it by matching each recorded cwd against the
transcript's own directory name. Not by un-slugging that name: the slug replaces
every `/` with `-`, so `-Users-me-work-devops-3179-onprem-failover-ui` is
irreversible. Matching has no such problem.

Related: a session's own transcript is the file whose stem is the session id.
Subagent turns live in `<id>/subagents/*.jsonl` beside it and carry the same
session id (with a `/sub` role suffix, since current versions no longer set
`isSidechain`), so "any path this session has rows in" is not good enough —
`session_rows` picks the session's own file deliberately.

## Deleting: partial failure is the interesting case

`delete_session` returns `(dropped, removed, errors)` and callers report all
three. A transcript we failed to `unlink` is **still on disk**, so its exclusion
row stays — dropping it would let the next indexing pass pull the session the user
just deleted straight back in. Only successfully removed paths are forgotten, and
the label survives while any file does, because there is still something to
restore.

## Parsing: assume nothing

Both CLIs document their transcript format as internal and reshape it between
releases. So:

- No dataclasses, no TypedDicts, no strict deserialisation for transcript
  entries. A test enforces this by AST.
- `type` is **not** reliably a string. JSON Schema fragments nest it as an object
  (`{"type": {"type": "string"}}`), which is unhashable and used to crash set
  membership. `parsers._kind` returns a string or None.
- `role` has been seen as a dict and as a list; `timestamp` as a dict. Everything
  bound for a column goes through `as_text`.
- One unparseable line must never abort a run. Failures are counted into
  `Report.skipped` (`RETRACE_VERBOSE=1` lists them).
- `harvest` does not descend into `input_schema`, `parameters`, `properties`,
  `tools`, `instructions`, `tool_choice`, `schema`, `$defs`, `definitions`. Two
  reasons: JSON Schema lives there, and Codex writes its whole system prompt into
  `instructions` on every session — indexing it would add hundreds of identical
  copies of the same text.
- Codex puts its kind marker at the top level in some versions and inside
  `payload` in others. Check both; `payload` wins. Getting this wrong keys
  sessions by filename and breaks resume.
- Project labels prefer a literal `cwd` from inside the transcript. Un-slugging a
  directory name is lossy: `-home-max-fingular-infra` reverses to
  `/home/max/fingular/infra`, which is wrong.

## Query semantics worth keeping

- **Quote every token.** Bare `haproxy.cfg` or `kube-proxy` is a *syntax error*
  in FTS5, not a zero-result query. If the input contains `AND OR NOT NEAR( ^ :`
  or a quote, it is passed through as FTS5 syntax verbatim.
- **Prefix mode by default.** A half-typed word is a prefix: `"mik"` matches
  nothing, `"mik"*` matches `mikrotik`. Exact matching in a live search box
  returns zero results until you finish the word. `search --exact` opts out.
- **No bm25 in the browser.** `ORDER BY bm25()` has to score every match: ~110ms
  on a broad query against ~2ms unordered. Date ordering is flat. Ranking is
  offered on `search`, where you read a top-20 list.
- **Never join `msgs` inside a count used as a guard.** That materialises every
  match; it once cost 1.28s per keystroke. `query.match_count` counts in the FTS
  index alone.
- **Role matching is `LIKE %user%`,** so sidechain turns (`user/sub`) count as
  user turns.
- **Titles come from one windowed query,** not one per session. The N+1 version
  was invisible at 25 rows and would be 475 round-trips at real scale.
- **A folder is an exact match; a typed filter is a substring.**
  `session_rows(project_exact=True)` for descending into a folder — otherwise
  `/work/api` swallows `/work/api-old`. The `--project` flag stays a substring,
  because that is what someone typing part of a path means.
- **An exact session id resolves without a window.** `resolve_session` looks it up
  unlimited: with the 40-row substring window applied, an id that also appears in
  40 newer sessions' paths resolves to nothing.
- **Names live outside the FTS index, so searching for one is a second query -
  and it runs alongside the text search, never instead of it.**
  `query.sessions_named` matches name/label/id/path, and both frontends show its
  results *above* the message hits. As a fallback ("only if no text hits") it was
  worse than useless: any conversation that quotes the name gives the text search
  hits, so the fallback stays quiet and the session actually *called* that is the
  one row missing. Pasting "run codex resume, then select thecultt-data-fixes"
  into any session is enough to trigger it. Do not "fix" the FTS side by inserting
  synthetic rows into `msgs` — that fabricates messages no transcript contains.
- **Values from the environment are validated, not trusted.** `config._int`
  rejects non-positive and non-numeric input, or `RETRACE_SUMMARY_TIMEOUT=-1`
  becomes a `ValueError` from inside `subprocess.run` several layers down.

## Performance budget

Real corpus: 475 sessions, 20,685 messages, 54MB index.

| operation | budget |
|---|---|
| `retrace s foo`, whole command | 60–95 ms |
| browser keystroke, typical query | 3–20 ms |
| browser keystroke, 1-letter prefix (14k matches) | ~33 ms |
| all sessions, uncapped | ~78 ms |
| no-op incremental index | ~11 ms |

Two things protect this and are easy to undo by accident:

- **`connect()` does no setup work.** It checks `PRAGMA user_version` and returns.
  No `executescript(SCHEMA)`, no `PRAGMA table_info` migration probe. Those were
  free behind 60ms of interpreter start-up and are not free now. A test sabotages
  `SCHEMA` and requires `connect()` not to reach it.
- **`snippet()` does not scale to a feed.** At 500 rows it tripled the cost of
  the keystroke query. The browser fetches `substr(text, 1, 1000)` and builds the
  excerpt in Python (`query.excerpt`); `snippet()` stays in the 20-row CLI search.

`FEED_LIMIT = 500` bounds *query cost*, not the display: a one-letter prefix
matches 14k messages and ordering them all costs ~520ms. Session and folder
listings are **not** capped — DataTable virtualises, and "only recent sessions
are reachable" was a real bug. Any truncation is reported in the status line
(`N of M matching messages`); silent caps are not acceptable.

## TUI notes

- View state is instance attributes on `RetraceApp` (`query_text`, `source`,
  `role`, `project`, `sort`, `view`). Not a database table.
- **No `priority=True` bindings.** A priority binding on the App outranks the
  focused widget, so Enter never reaches a modal's Input and Escape never reaches
  a dialog. Screen bindings already win over App bindings.
- **Escape does not quit.** It clears the query, then the filters. Quitting is
  `ctrl+q` (Textual's own binding, declared explicitly so the footer shows it).
  `ctrl+c` is Input's copy while the search box has focus, so it does not quit
  either — which is worth documenting rather than "fixing".
- **Don't bind keys `Input` owns** — `ctrl+a/c/d/e/k/u/v/w/x`, `delete`, arrows.
  A focused search box eats them first. That is why delete is `f8` and help is
  `f1` (`?` is a character someone may want to search for). A test cross-checks
  `RetraceApp.BINDINGS` against `Input.BINDINGS`.
- **Resume happens after the app exits.** `os.execvp` replaces the process; doing
  it while Textual owns the terminal leaves raw mode behind. The app returns
  `("resume", row)` and `run_app` acts on it. Same for `("print", text)`.
- **Any child process goes through `RetraceApp.run_external`,** which wraps
  `with self.suspend():` and handles the failure. Textual is pinned to an *exact*
  version because suspend/restore has regressed across minor releases and a
  failure there leaves the user's terminal wedged. If you bump it, run the suite
  and then actually suspend into `$PAGER` in a real terminal.
- Long work (summarising can take minutes) runs in a thread worker with **its
  own** sqlite connection — a connection belongs to the thread that made it.
- `chdir` before resume is required, not cosmetic: Claude Code scopes session-id
  lookup to the current project directory and its worktrees, and otherwise says
  `No conversation found with session ID`.

## Things we assume and cannot verify

Treat these as assumptions with fallbacks, and keep the fallbacks working.
`tests/test_unverified.py` tests the fallbacks, not the assumptions.

1. `codex resume <id>` is the right invocation → `RETRACE_CODEX_RESUME` is a
   template.
2. `codex exec` uses stdin as conversation context. It certainly *reads* stdin;
   the rest is inferred → `--with claude` switches engines, and a failing child
   is always surfaced, never silently turned into a label.
3. ~~A CLI-set session name may not exist in the JSONL at all.~~ **Settled, and
   it is both.** Claude Code records `/rename` in the transcript as a command
   entry - in a `type: "system"` entry with no message body, which is why it is
   matched on the raw line rather than in extracted text, and why the pattern
   insists on the `<command-message>rename</command-message>` middle element: a
   conversation that merely quotes the markup is not a rename. Codex keeps names
   in a sibling `session_index.jsonl` (`{id, thread_name, updated_at}`), so for
   Codex no amount of transcript reading would ever have found them.
4. `less` behaviour: `-R +N` is added only when the pager basename is `less`;
   a missing pager writes the text out instead.
5. Renaming: Claude Code's real session names are only settable interactively or
   at start-up. The only other route is editing the single copy of a transcript
   to change a display string, which we will not do. `retrace name` stores a
   label in our own database and we *read* a CLI-set name if one exists.

## CLI ergonomics

`name` takes a trailing `nargs="*"` label. argparse before 3.12 cannot resume a
trailing positional after an option, so `name 1 --source claude some words` is a
usage error on 3.11 and works on 3.12+. Quote the label and put flags last.

## Screenshots

`docs/*.svg` are generated by `docs/make_screenshot.py` from an invented corpus,
and they must stay that way. A screenshot of this tool taken against a real index
publishes the author's own conversations, project paths and pasted files.

## Testing

```sh
uv sync --extra dev && uv run pytest        # ~30s, 236 tests
uv run --python 3.11 --extra dev pytest     # CI runs 3.11/3.12/3.13
```

- `tests/conftest.py` builds synthetic transcripts in both formats, including
  hostile ones (dict-valued `type`, list-valued `role`, partial trailing lines,
  JSON Schema blobs, a Codex file with the kind marker at top level).
- Anything that shells out is tested against recording stub binaries on `PATH`
  (argv, stdin, cwd, exit codes, timeouts). `claude`, `codex` and `less` are
  **not** required to run the suite, and tests must not start depending on them —
  the machine you develop on probably has `claude` installed, so a test that
  forgets to override a template passes locally and fails in CI.
- The TUI is driven through Textual's `Pilot`; every binding has a test.
- `tests/test_invariants.py` and `tests/test_regressions.py` are named after the
  rules above and the bugs below. Add to them rather than starting new files.

## Bugs that already happened

Don't reintroduce these. Each has a test.

The numbers are the `test_rNN_*` prefixes in `tests/test_regressions.py`.

| # | bug | cause |
|---|---|---|
| 1 | `TypeError: unhashable type: 'dict'` while indexing | `type` was a JSON Schema object |
| 2 | crash on non-string `role` / `timestamp` | fields are not type-stable |
| 3 | one bad line aborted the whole run | no per-line isolation |
| 4 | search for `mik` found nothing while `mikrotik` worked | quoted tokens are whole-token matches |
| 5 | cancelling an action reported as failure | cancellation is exit 0, or 130 for an interrupt |
| 6 | Codex sessions keyed by filename, resume broken | kind marker read from one level only |
| 7 | keystroke latency 65ms → 1.28s | a count query joined `msgs` to apply a filter |
| 8 | every summary claimed `(clipped)` | two return values packed into one |
| 9 | `codex exec` exited 1 | missing `--skip-git-repo-check` |
| 10 | status/header text overflowed | budgeted against terminal width, not pane width |
| 11 | only recent sessions reachable | the browse list showed newest *messages*, not sessions |
| 12 | project shown as `/home/max/fingular/infra` | dash-slug is lossy; prefer a literal `cwd` |
| 13 | `--source` silently ignored | accepted by argparse, never threaded into the query |

## Style

- Comments explain *why*, and specifically why the obvious thing is wrong. A
  comment that restates the code is noise; a comment recording a measurement or a
  crash is the most valuable thing in the file.
- Keep the fast half stdlib-only. No Rich in `cli.py`/`render.py`, no Textual
  outside `tui/`.
- British spelling in prose is fine; don't churn existing text either way.
