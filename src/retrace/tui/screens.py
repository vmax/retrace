"""Modal dialogs.

A Textual app owns the terminal, so a confirmation is a dialog rather than a
prompt written to ``/dev/tty``.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Static


class ConfirmDelete(ModalScreen[str | None]):
    """Delete confirmation. Returns ``"purge"``, ``"index"`` or None.

    Destructive actions confirm and fail closed: escape, or dismissing this any
    other way, cancels.
    """

    BINDINGS = [
        Binding("escape", "cancel", "cancel"),
        Binding("y", "purge", "delete file"),
        Binding("i", "index_only", "index only"),
    ]

    def __init__(self, row: dict, paths: list[str]):
        super().__init__()
        self.row = row
        self.paths = paths

    def compose(self) -> ComposeResult:
        r = self.row
        with Vertical(id="dialog"):
            yield Label("Delete this session?", id="dialog-title")
            yield Static(f"[b]{r['title'][:100]}[/b]")
            yield Static(f"{r['source']}  {r['session']}  {r['n']} messages")
            for p in self.paths:
                yield Static(f"  {p}", classes="path")
            yield Static(
                "[b red]Deleting the transcript is irreversible - "
                "it is the only copy of this conversation.[/]"
            )
            with Horizontal(id="dialog-buttons"):
                yield Button("Delete file (y)", variant="error", id="purge")
                yield Button("Index only (i)", variant="warning", id="index")
                yield Button("Cancel (esc)", variant="primary", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(None if event.button.id == "cancel" else event.button.id)

    def action_purge(self) -> None:
        self.dismiss("purge")

    def action_index_only(self) -> None:
        self.dismiss("index")

    def action_cancel(self) -> None:
        self.dismiss(None)


class RenameSession(ModalScreen[str | None]):
    """Set or clear a session label. Empty input clears it; escape keeps it."""

    BINDINGS = [Binding("escape", "cancel", "cancel")]

    def __init__(self, row: dict, current: str | None):
        super().__init__()
        self.row = row
        self.current = current or ""

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("Label this session", id="dialog-title")
            yield Static(f"{self.row['source']}  {self.row['session']}")
            yield Static("[dim]stored in retrace's database; "
                         "the transcript is not touched[/]")
            yield Input(value=self.current, placeholder="empty to clear",
                        id="label-input")

    def on_mount(self) -> None:
        self.query_one("#label-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip())

    def action_cancel(self) -> None:
        self.dismiss(None)


class TextScreen(ModalScreen[str | None]):
    """A scrollable block of text: the summary, and the key help."""

    BINDINGS = [
        Binding("escape,q", "close", "close"),
        Binding("l", "label", "save as label"),
    ]

    def __init__(self, title: str, body: str, offer_label: bool = False):
        super().__init__()
        self.title_text = title
        self.body = body
        self.offer_label = offer_label

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self.title_text, id="dialog-title")
            with VerticalScroll(id="text-body"):
                yield Static(self.body, id="text-content")
            hint = "esc close" + ("  ·  l save as label" if self.offer_label else "")
            yield Static(f"[dim]{hint}[/]")

    def action_close(self) -> None:
        self.dismiss(None)

    def action_label(self) -> None:
        self.dismiss(self.body if self.offer_label else None)
