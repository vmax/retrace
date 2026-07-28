"""The Textual browser.

Nothing outside this package may import it: `retrace.cli` imports `.app` inside
the body of the interactive command so that the fast subcommands never pay
Textual's 300-500ms import cost.
"""
