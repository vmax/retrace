"""retrace - local full-text search over Claude Code and Codex CLI transcripts.

Importing this package must stay cheap: it pulls in nothing but the standard
library. Textual is imported lazily, inside the interactive command only, because
importing it costs 300-500ms and every non-interactive subcommand is expected to
finish in about a tenth of that.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
