"""The browser.

One long-lived process, so view state is five instance attributes and every
action is a method call. Two structural consequences worth knowing before
changing anything here: a resume has to happen *after* the app exits (execvp
would inherit raw mode), and every child process goes through `run_external`,
which is the only place `suspend()` is called.
"""

from __future__ import annotations

import sqlite3

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import DataTable, Footer, Input, Static

from .. import actions, query, sessions, storage
from ..query import ROLE_CYCLE
from .screens import ConfirmDelete, RenameSession, TextScreen

#: Typing pause before a query runs. Queries are 3-35ms in-process, so this is
#: about not querying on every single keypress, not about hiding latency.
DEBOUNCE = 0.09

PROJECT_PREFIX = "proj:"

#: Named sessions shown above the message hits. A name match is precise, so this
#: only ever bounds a pathological substring like "a".
NAMED_LIMIT = 25

HELP = """\
[b]Navigating[/b]
  type            search message text (every word is a prefix)
  up/down/pgup    move the selection
  enter           resume the selected session — on a folder row, descend into it
  escape          clear the query, then the filters — never exits
  ctrl+q          quit
  f1              this help

[b]Acting on the selection[/b]
  ctrl+s          read the whole session in $PAGER, positioned on the hit
  alt+s           summarise the session with claude/codex
  ctrl+r          label the session (stored by retrace; the transcript is not touched)
  f8              delete the session (confirms first)
  ctrl+o          print the selected message to stdout and quit

[b]In the search box[/b]
  ctrl+c          copy · ctrl+x cut · ctrl+v paste · ctrl+u clear · ctrl+w
                  delete word. These belong to the text field, so ctrl+c does
                  not quit while you are typing — ctrl+q does.

[b]Filtering[/b]
  alt+p           switch between folder and session level
  alt+a/c/x       source: all / claude / codex
  alt+u           cycle role: any → user → assistant
  alt+d           flip newest ⇄ oldest
  alt+e           show/hide a collapsed initial prompt in the preview
  … title         session starts with a collapsed setup section

[b]What is indexed[/b]
  User and assistant text only. Tool calls and results are excluded by default
  because they contain file reads and terminal output — run
  `retrace index --include-tools` to opt in.
"""


class RetraceApp(App):
    TITLE = "retrace"

    CSS = """
    Screen { layers: base overlay; }
    #status { height: 1; padding: 0 1; background: $panel; color: $text-muted; }
    #q { border: none; height: 3; }
    #body { height: 1fr; }
    #list { width: 3fr; }
    #preview { width: 2fr; border-left: solid $panel; padding: 0 1; }
    #preview-content { width: 100%; }
    DataTable { height: 1fr; }
    #dialog {
        width: 80%; max-width: 100; height: auto; max-height: 80%;
        padding: 1 2; background: $surface; border: thick $primary;
    }
    #dialog-title { text-style: bold; margin-bottom: 1; }
    #dialog-buttons { height: auto; margin-top: 1; }
    #dialog-buttons Button { margin-right: 1; }
    #text-body { height: 1fr; max-height: 30; margin: 1 0; }
    .path { color: $text-muted; }
    """

    # No `priority=True` anywhere: a priority binding on the App outranks the
    # focused widget, so Enter never reaches a modal's Input and Escape never
    # reaches a dialog. Screen bindings already win over App bindings, which is
    # exactly the behaviour the dialogs need.
    BINDINGS = [
        Binding("ctrl+s", "pager", "read"),
        Binding("alt+s", "summarize", "sum"),
        Binding("ctrl+r", "rename", "label"),
        # f8, and not ctrl+x or ctrl+d: both are bound by Input (cut and
        # delete-right), so the focused search box would swallow them before the
        # app saw them - and ctrl+x also sits next to alt+x, "filter to codex",
        # which is a bad neighbour for a delete key.
        Binding("f8", "delete", "delete"),
        # print and clear are in the help and the status line; the footer is a
        # fixed list that truncates, so only what fits goes in it
        Binding("ctrl+o", "print_message", "print", show=False),
        Binding("alt+p", "flip_view", "folders"),
        # The filter keys are show=False here and rendered in the status line
        # instead. The footer is a fixed list that silently truncates: with these
        # in it, alt+x, alt+u and alt+d fell off the end at 118 columns and simply
        # did not exist as far as a user could tell. The status line is ours, it
        # already reports the current scope, and it can trim to fit on purpose.
        Binding("alt+a", "source('all')", "both", show=False),
        Binding("alt+c", "source('claude')", "claude", show=False),
        Binding("alt+x", "source('codex')", "codex", show=False),
        Binding("alt+u", "cycle_role", "role", show=False),
        Binding("alt+d", "flip_sort", "order", show=False),
        Binding("alt+e", "toggle_initial_prompt", "initial prompt", show=False),
        # f1, not "?": "?" is a character someone may want to search for, and the
        # search box has focus by default
        Binding("f1,question_mark", "help", "keys"),
        # Quitting is an explicit key. Escape only clears the query: it is the
        # key you hit reflexively after a mistyped search, and "clear, or exit the
        # whole application if the box happened to be empty" is a coin flip on
        # something that cannot be undone.
        Binding("ctrl+q", "quit", "quit"),
        Binding("escape", "escape", "clear", show=False),
        Binding("down", "cursor('down')", show=False),
        Binding("up", "cursor('up')", show=False),
        Binding("pagedown", "cursor('pagedown')", show=False),
        Binding("pageup", "cursor('pageup')", show=False),
    ]

    def __init__(self, db: sqlite3.Connection, *, query_text: str = "",
                 source: str = "all", role: str = "all", project: str = "",
                 sort: str = "new", view: str = "sessions", dry_run: bool = False):
        super().__init__()
        self.db = db
        # This is the state that used to live in a `meta` table.
        self.query_text = query_text
        self.source = source or "all"
        self.role = role or "all"
        self.project = project or ""
        # A folder descent means *that* folder; a --project flag is a substring
        # the user typed. /work/api must not drag in /work/api-old.
        self.project_exact = False
        self.sort = sort or "new"
        self.view = view
        self.dry_run = dry_run
        self.rows: dict[str, dict] = {}
        self._debounce = None
        self._total = 0
        self._truncated = False
        self._by_name = False
        self._named = 0
        # Presentation state only: opening a bootstrap prompt must not mutate
        # the transcript or leak into another invocation of the browser.
        self.expanded_initial_prompts: set[str] = set()

    # ------------------------------------------------------------------ layout

    def compose(self) -> ComposeResult:
        yield Static(id="status")
        yield Input(value=self.query_text, placeholder="search messages…", id="q")
        with Horizontal(id="body"):
            with Vertical(id="list"):
                yield DataTable(id="table", cursor_type="row", zebra_stripes=True)
            with VerticalScroll(id="preview"):
                yield Static(id="preview-content")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#q", Input).focus()
        self.refresh_rows()

    # ------------------------------------------------------------------- input

    @on(Input.Changed, "#q")
    def _on_change(self, event: Input.Changed) -> None:
        self.query_text = event.value
        if self._debounce is not None:
            self._debounce.stop()
        self._debounce = self.set_timer(DEBOUNCE, self.refresh_rows)

    @on(Input.Submitted, "#q")
    def _on_submit(self) -> None:
        """Enter in the search box acts on the selection."""
        self.action_activate()

    @on(DataTable.RowSelected)
    def _on_row_selected(self) -> None:
        self.action_activate()

    def action_cursor(self, direction: str) -> None:
        table = self.query_one("#table", DataTable)
        {"down": table.action_cursor_down, "up": table.action_cursor_up,
         "pagedown": table.action_page_down, "pageup": table.action_page_up}[direction]()

    def action_escape(self) -> None:
        """Clear the query. Never exit - that is ctrl+q."""
        if self.query_text:
            self.query_one("#q", Input).value = ""
            return
        if self.project or self.view == "projects" or self.source != "all" \
                or self.role != "all":
            self.notify("filters cleared · ctrl+q to quit")
            self.project = ""
            self.project_exact = False
            self.view = "sessions"
            self.source = "all"
            self.role = "all"
            self.refresh_rows()
            return
        self.notify("ctrl+q to quit")

    # ------------------------------------------------------------------- state

    def action_source(self, value: str) -> None:
        self.source = value
        self.refresh_rows()

    def action_cycle_role(self) -> None:
        cur = self.role if self.role in ROLE_CYCLE else "all"
        self.role = ROLE_CYCLE[(ROLE_CYCLE.index(cur) + 1) % len(ROLE_CYCLE)]
        self.refresh_rows()

    def action_flip_sort(self) -> None:
        self.sort = "old" if self.sort == "new" else "new"
        self.refresh_rows()

    def action_flip_view(self) -> None:
        if self.view == "projects":
            self.view = "sessions"
        else:
            self.view = "projects"
            self.project = ""        # going up clears the folder filter
            self.project_exact = False
        self.refresh_rows()

    def action_toggle_initial_prompt(self) -> None:
        """Show or hide the selected session's oversized bootstrap prompt."""
        row = self.current_session()
        if row is None:
            self.notify("select a session to toggle its initial prompt")
            return
        messages = sessions.messages(self.db, row["session"])
        if self._initial_prompt_end(messages) is None:
            self.notify("this session has no collapsed initial prompt")
            return
        if row["session"] in self.expanded_initial_prompts:
            self.expanded_initial_prompts.remove(row["session"])
        else:
            self.expanded_initial_prompts.add(row["session"])
        self.update_preview()

    def action_help(self) -> None:
        self.push_screen(TextScreen("retrace keys", HELP))

    # -------------------------------------------------------------------- rows

    def refresh_rows(self) -> None:
        """Rebuild the list from the current state."""
        self._debounce = None
        table = self.query_one("#table", DataTable)
        table.clear(columns=True)
        self.rows = {}
        self._truncated = False
        self._total = 0
        self._by_name = False

        try:
            if self.view == "projects":
                self._fill_projects(table)
            elif self.query_text.strip():
                self._fill_messages(table)
            else:
                self._fill_sessions(table)
        except query.BadQuery:
            table.add_columns("")
            table.add_row(Text("incomplete query", style="italic"))
        self.update_status()
        self.update_preview()

    def _fill_projects(self, table: DataTable) -> None:
        table.add_columns("last", "cli", "sessions", "msgs", "folder")
        needle = self.query_text.strip().lower()
        for r in query.project_rows(
            self.db, source=self.source, sort=self.sort, limit=None
        ):
            if needle and needle not in r["project"].lower():
                continue
            key = PROJECT_PREFIX + r["project"]
            self.rows[key] = {"kind": "project", **r}
            table.add_row(
                (r["last"] or "")[:10],
                "both" if "," in r["sources"] else r["sources"],
                str(r["sessions"]), str(r["messages"]), r["project"],
                key=key,
            )

    def _fill_sessions(self, table: DataTable) -> None:
        self._fill_session_rows(table, query.session_rows(
            self.db, limit=None,           # DataTable virtualises; show them all
            project=self.project or None,
            project_exact=self.project_exact,
            source=None if self.source == "all" else self.source,
            sort=self.sort,
        ))

    def _fill_session_rows(self, table: DataTable, rows: list[dict]) -> None:
        table.add_columns("last", "cli", "msgs", "folder", "title")
        self._total = len(rows)
        for r in rows:
            key = f"sess:{r['session']}"
            self.rows[key] = {"kind": "session", **r}
            table.add_row(
                (r["last"] or "")[:10], r["source"][:6], str(r["n"]),
                short_project(r["project"]),
                self._session_title(r),
                key=key,
            )

    @staticmethod
    def _session_title(row: dict) -> Text:
        """A compact, visible marker for previews with hidden bootstrap text."""
        title = Text()
        if row.get("has_collapsed_initial"):
            title.append("… ", style="dim")
        if row["label"]:
            title.append("* ")
        title.append(row["title"])
        return title

    def _fill_messages(self, table: DataTable) -> None:
        rows = query.message_feed(
            self.db, self.query_text, source=self.source, role=self.role,
            project=self.project, project_exact=self.project_exact, sort=self.sort,
        )
        # Sessions *called* this, above the messages that mention it. Additive, not
        # a fallback: as soon as any conversation quotes the name the text search
        # has hits, and the session named that would be the one row missing.
        named = query.sessions_named(
            self.db, self.query_text,
            source=None if self.source == "all" else self.source,
            project=self.project or None, project_exact=self.project_exact,
            sort=self.sort, limit=NAMED_LIMIT,
        )
        self._named = len(named)
        self._by_name = bool(named) and not rows

        table.add_columns("when", "cli", "role", "folder", "message")
        for r in named:
            key = f"sess:{r['session']}"
            self.rows[key] = {"kind": "session", **r}
            table.add_row(
                (r["last"] or "")[:10], r["source"][:6], "session",
                short_project(r["project"]),
                Text(("* " if r["label"] else "") + r["title"], style="bold"),
                key=key,
            )
        self._total = query.match_count(self.db, self.query_text)
        self._truncated = len(rows) >= query.FEED_LIMIT
        for r in rows:
            key = f"msg:{r['id']}"
            if key in self.rows:            # defensive: keys are unique per kind
                continue
            self.rows[key] = {"kind": "message", **r}
            table.add_row(
                (r["ts"] or "")[:10], r["source"][:6], (r["role"] or "")[:9],
                short_project(r["project"]), highlight(r["excerpt"], r["match_at"],
                                                       self.query_text),
                key=key,
            )

    def update_status(self) -> None:
        scope = self.source if self.source in ("claude", "codex") else "claude+codex"
        if self.role in ("user", "assistant"):
            scope += f" {self.role}"
        if self.project:
            scope += f" in *{self.project}*"
        what = "oldest" if self.sort == "old" else "newest"
        shown = len(self.rows)
        if self.view == "projects":
            head = f"{shown} folders"
        elif self.query_text.strip():
            msgs = shown - self._named
            head = f"{msgs} of {self._total} matching messages"
            if self._truncated:
                head += " (refine to see more)"
            if self._named:
                head = f"{self._named} named session(s) · " + head
        else:
            head = f"{shown} sessions"

        widget = self.query_one("#status", Static)
        state = [head, scope, f"{what} first"]
        widget.update(fit(state + self.filter_legend(),
                          max(20, widget.size.width or self.size.width) - 2))

    def filter_legend(self) -> list[str]:
        """Keys that change what is listed, each naming where it takes you.

        Toggles name their destination, so the label changes with the state:
        `alt+d oldest` while sorted newest first.
        """
        legend = []
        if self.source != "claude":
            legend.append("alt+c claude")
        if self.source != "codex":
            legend.append("alt+x codex")
        if self.source != "all":
            legend.append("alt+a both")
        if self.view != "projects":
            cur = self.role if self.role in ROLE_CYCLE else "all"
            nxt = ROLE_CYCLE[(ROLE_CYCLE.index(cur) + 1) % len(ROLE_CYCLE)]
            legend.append(f"alt+u {'any role' if nxt == 'all' else nxt}")
        legend.append("alt+d " + ("newest" if self.sort == "old" else "oldest"))
        legend.append("f1 keys")
        return legend

    # ----------------------------------------------------------------- preview

    @on(DataTable.RowHighlighted)
    def _on_highlight(self) -> None:
        self.update_preview()

    def current(self) -> dict | None:
        table = self.query_one("#table", DataTable)
        if table.row_count == 0 or table.cursor_row < 0:
            return None
        try:
            key = table.coordinate_to_cell_key((table.cursor_row, 0)).row_key.value
        except Exception:                      # table mid-rebuild
            return None
        return self.rows.get(key)

    def current_session(self) -> dict | None:
        """The session behind the selection, whatever kind of row it is."""
        row = self.current()
        if row is None or row["kind"] == "project":
            return None
        if row["kind"] == "session":
            return row
        return sessions.session_by_id(self.db, row["session"])

    def update_preview(self) -> None:
        """Render the pane beside the list for whatever is selected."""
        pane = self.query_one("#preview-content", Static)
        row = self.current()
        if row is None:
            pane.update("")
            return
        if row["kind"] == "project":
            pane.update(self._preview_project(row))
        elif row["kind"] == "session":
            pane.update(self._preview_session(row))
        else:
            pane.update(self._preview_message(row))

    def _preview_project(self, row: dict) -> Text:
        out = Text()
        out.append(f"{row['project']}\n", style="bold")
        out.append(f"{row['sessions']} sessions, {row['messages']} messages "
                   f"- enter to descend\n\n", style="dim")
        for r in query.session_rows(self.db, limit=40, project=row["project"],
                                    project_exact=True):
            out.append(f"{(r['last'] or '')[:10]}  {r['source'][:6]:6} "
                       f"{r['n']:>4}m  ", style="dim")
            out.append(f"{r['title'][:70]}\n")
        return out

    def _preview_session(self, row: dict) -> Text:
        all_messages = sessions.messages(self.db, row["session"])
        collapsed_end = self._initial_prompt_end(all_messages)
        is_expanded = row["session"] in self.expanded_initial_prompts
        title = row["title"]
        # The default title is the first user message. Do not let that one line
        # defeat the collapse, while preserving explicit retrace/CLI names.
        if collapsed_end is not None and not is_expanded \
                and title == query.clean_title(all_messages[0][3]):
            title = "(initial prompt collapsed)"
        out = Text()
        out.append(f"{title}\n", style="bold")
        out.append(f"{row['source']}  {row['session']}\n{row['project'] or '-'}\n"
                   f"{(row['first'] or '')[:19]} .. {(row['last'] or '')[:19]}  "
                   f"{row['n']} messages\n\n", style="dim")
        if collapsed_end is not None and not is_expanded:
            collapsed_chars = sum(len(message[3] or "")
                                  for message in all_messages[:collapsed_end])
            out.append(f"{collapsed_end} initial messages collapsed · "
                       f"{collapsed_chars:,} chars · alt+e to expand\n\n", style="dim")
        messages = all_messages if is_expanded or collapsed_end is None \
            else all_messages[collapsed_end:]
        for role, ts, _line, text in messages[:12]:
            style = message_style(role)
            out.append(f"{role} ", style=f"bold {style}")
            out.append(f"{(ts or '')[:19]}\n", style="dim")
            out.append(text[:1200].strip() + "\n\n", style=style)
        return out

    @staticmethod
    def _initial_prompt_end(messages: list[tuple]) -> int | None:
        """Return the bootstrap prefix ending before the first real request.

        Collapsing later long requests would conceal the conversation's useful
        work. Restricting this to the first substantive user turn also avoids
        trying to recognise fragile, CLI-specific skill or agent wrapper formats.
        Once that turn is known to be bootstrap, hide its setup response(s) as well:
        the next substantive user turn is where someone resumes reading.
        """
        bootstrap_at = None
        for i, (role, _ts, _line, text) in enumerate(messages):
            if str(role).split("/", 1)[0] != "user" or not query.clean_title(text):
                continue
            if query.is_initial_bootstrap_text(text):
                bootstrap_at = i
            break
        if bootstrap_at is None:
            return None
        last_bootstrap = bootstrap_at
        for i, (later_role, _ts, _line, later_text) in enumerate(
                messages[bootstrap_at + 1:], bootstrap_at + 1):
            if str(later_role).split("/", 1)[0] != "user" \
                    or not query.clean_title(later_text):
                continue
            if query.is_known_bootstrap_preamble(later_text):
                last_bootstrap = i
                continue
            return i
        # Repeated injected handoffs are one setup section, not a conversation.
        return len(messages) if last_bootstrap > bootstrap_at else bootstrap_at + 1

    def _preview_message(self, row: dict) -> Text:
        head, ctx = sessions.context_around(self.db, row["id"])
        out = Text()
        if head is None:
            return out
        out.append(f"{head['source']}  {head['session']}\n", style="bold")
        out.append(f"{head['project']}\n{head['path']}:{head['line']}\n\n", style="dim")
        for role, ts, line, text in ctx:
            marker = ">>> " if line == head["line"] else "    "
            style = message_style(role)
            out.append(marker + str(role), style=f"bold {style}")
            out.append(f" {(ts or '')[:19]}\n", style="dim")
            out.append(text[:2500].strip() + "\n\n", style=style)
        return out

    # ----------------------------------------------------------------- actions

    def action_activate(self) -> None:
        row = self.current()
        if row is None:
            return
        if row["kind"] == "project":
            # the same key means "descend" up here and "resume" one level down
            self.project = row["project"]
            self.project_exact = True
            self.view = "sessions"
            self.query_one("#q", Input).value = ""
            self.query_text = ""
            self.refresh_rows()
            return
        r = self.current_session()
        if r is None:
            return
        # Resume replaces this process, so it has to happen after the app has
        # released the terminal: hand it back to the caller.
        self.exit(("resume", r))

    def action_print_message(self) -> None:
        row = self.current()
        if row is None:
            return
        if row["kind"] == "message":
            text = self.db.execute(
                "SELECT text FROM msgs WHERE id=?", (row["id"],)).fetchone()
            self.exit(("print", text[0] if text else ""))
        else:
            r = self.current_session()
            if r is not None:
                text, _ = sessions.render_session(self.db, r)
                self.exit(("print", text))

    def action_pager(self) -> None:
        """Read the session in $PAGER, positioned on the hit."""
        row = self.current()
        r = self.current_session()
        if r is None:
            return
        mark = row["line"] if row["kind"] == "message" else None
        text, jump = sessions.render_session(self.db, r, mark_line=mark)
        self.run_external(lambda: actions.page(text, max(1, jump - 2)))

    def run_external(self, fn) -> None:
        """Hand the terminal to a child process and take it back afterwards.

        `suspend()` is the whole reason Textual is pinned to an exact version:
        its suspend/restore path has regressed across minor releases, and a
        failure here leaves the user staring at a wedged terminal. So the failure
        path is handled rather than assumed away.
        """
        try:
            with self.suspend():
                fn()
        except Exception as e:                 # SuspendNotSupported, OSError, ...
            self.notify(f"could not hand over the terminal: {e}",
                        severity="error", timeout=8)
        finally:
            self.refresh()

    def action_rename(self) -> None:
        r = self.current_session()
        if r is None:
            return

        def done(name: str | None) -> None:
            if name is None:
                return
            if name:
                sessions.set_label(self.db, r["session"], name)
                self.notify(f"labelled: {name[:60]}")
            else:
                sessions.clear_label(self.db, r["session"])
                self.notify("label cleared")
            self.refresh_rows()

        self.push_screen(RenameSession(r, sessions.get_label(self.db, r["session"])),
                         done)

    def action_delete(self) -> None:
        r = self.current_session()
        if r is None:
            return
        paths = sessions.session_paths(self.db, r["session"])

        def done(choice: str | None) -> None:
            if choice is None:
                self.notify("cancelled")
                return
            n, gone, errors = sessions.delete_session(
                self.db, r, purge=choice == "purge")
            for e in errors:
                self.notify(f"could not remove {e}", severity="error")
            self.notify(
                f"dropped {n} messages"
                + (f", deleted {gone} file(s)" if gone else ", transcript kept"),
                severity="warning",
            )
            self.refresh_rows()

        self.push_screen(ConfirmDelete(r, paths), done)

    def action_summarize(self) -> None:
        r = self.current_session()
        if r is None:
            return
        self.notify(f"summarising {r['n']} messages with {r['source']}…", timeout=6)
        self.summarize_worker(r)

    @work(thread=True, exclusive=True, group="summarize")
    def summarize_worker(self, r: dict) -> None:
        """Runs off the UI thread: a summary can take minutes.

        Its own connection, because a sqlite3 connection belongs to the thread
        that made it. connect() is cheap now, which is what makes this fine.
        """
        db = storage.connect()
        try:
            summary = actions.summarize_session(db, r)
        except actions.ActionError as e:
            self.call_from_thread(self.notify, str(e), severity="error", timeout=10)
            return
        finally:
            db.close()
        self.call_from_thread(self.show_summary, r, summary)

    def show_summary(self, r: dict, summary: str) -> None:
        def done(label: str | None) -> None:
            if not label:
                return
            import re
            first = re.sub(r"\s+", " ", label).strip()[:120]
            sessions.set_label(self.db, r["session"], first)
            self.notify(f"labelled: {first[:60]}")
            self.refresh_rows()

        self.push_screen(
            TextScreen(f"summary · {r['title'][:60]}", summary, offer_label=True),
            done,
        )


# ----------------------------------------------------------------------- helpers

SEP = " · "


def message_style(role: str | None) -> str:
    """Keep the two conversational voices legible in long previews."""
    primary = str(role or "").split("/", 1)[0]
    if primary == "user":
        return "cyan"
    if primary == "assistant":
        return "green"
    return "dim"


def fit(parts: list[str], width: int) -> str:
    """Join what fits, dropping from the end rather than truncating mid-word."""
    out: list[str] = []
    used = 0
    for part in parts:
        need = len(part) + (len(SEP) if out else 0)
        if out and used + need > width:
            break
        out.append(part)
        used += need
    return SEP.join(out)


def short_project(project: str | None) -> str:
    return (project or "-").rstrip("/").split("/")[-1][:18]


def highlight(text: str, at: int, q: str) -> Text:
    """Bold the matched term in a list row."""
    out = Text(text)
    if at >= 0:
        terms = query.query_terms(q)
        width = max((len(t) for t in terms), default=0)
        if width:
            out.stylize("bold yellow", at, at + width)
    return out


def run_app(db: sqlite3.Connection, args) -> int:
    """Run the browser, then perform whatever it handed back.

    resume has to happen out here: `os.execvp` replaces the process, and doing
    that while Textual still owns the terminal would leave it in raw mode.
    """
    app = RetraceApp(
        db,
        query_text=getattr(args, "query", "") or "",
        source=getattr(args, "source", None) or "all",
        role=getattr(args, "role", None) or "all",
        project=getattr(args, "project", None) or "",
        sort=getattr(args, "sort", None) or "new",
        view="projects" if getattr(args, "projects", False) else "sessions",
        dry_run=getattr(args, "dry_run", False),
    )
    result = app.run()
    if not result:
        return 0
    what, payload = result
    if what == "print":
        print(payload)
        return 0
    if what == "resume":
        try:
            print(f"$ {actions.resume_display(payload)}")
            actions.do_resume(payload, dry_run=app.dry_run)
        except actions.ActionError as e:
            print(str(e))
            return 1
    return 0
