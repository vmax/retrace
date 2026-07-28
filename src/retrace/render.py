"""ANSI output for the non-interactive commands.

Hand-rolled escapes rather than Rich: this module is imported by every CLI
invocation and the whole point of the split is that the fast half stays
stdlib-only.
"""

from __future__ import annotations

import os
import sys


class Palette:
    __slots__ = ("bold", "dim", "hl", "off")

    def __init__(self, color: bool):
        if color:
            self.bold, self.dim, self.hl, self.off = (
                "\033[1m", "\033[2m", "\033[1;33m", "\033[0m")
        else:
            self.bold = self.dim = self.hl = self.off = ""

    def as_tuple(self):
        return self.bold, self.dim, self.off


def palette(no_color: bool = False) -> Palette:
    color = (
        not no_color
        and sys.stdout.isatty()
        and not os.environ.get("NO_COLOR")
    )
    return Palette(color)


def session_list(rows: list[dict], pal: Palette, numbered: bool = True) -> str:
    out = []
    for i, r in enumerate(rows, 1):
        num = f"{i:>3}  " if numbered else ""
        tag = "* " if r.get("label") else ""      # * marks a label we set
        out.append(f"{num}{pal.bold}{tag}{r['title'][:96]}{pal.off}")
        out.append(
            f"     {pal.dim}{r['source']:6} {(r['last'] or '')[:16]}  "
            f"{r['n']:>4} msgs  {r['project'] or '-'}{pal.off}"
        )
        out.append(f"     {pal.dim}{r['session']}{pal.off}")
    return "\n".join(out)


def hits(rows: list[dict], pal: Palette) -> str:
    out = []
    for r in rows:
        snip = (r["snippet"] or "").replace("{HL}", pal.hl).replace("{OFF}", pal.off)
        snip = snip.replace("\n", " ")
        out.append(
            f"{pal.bold}{r['source']}:{r['role']}{pal.off} "
            f"{pal.dim}{(r['ts'] or '')[:19]}  {r['project'] or '-'}{pal.off}"
        )
        out.append(f"  {snip}")
        out.append(
            f"  {pal.dim}{r['path']}:{r['line']}  session={r['session']}  "
            f"bm25={r['score']:.2f}{pal.off}\n"
        )
    return "\n".join(out)
