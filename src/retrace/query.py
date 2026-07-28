"""Query construction: FTS5 preparation, message search, session and project lists.

Two performance facts are load-bearing here and both were measured:

* ``ORDER BY bm25()`` has to score every match, so a broad query cost ~110ms
  against ~2ms unordered. Date ordering is flat ~100ms with no pathological
  case, so the live picker orders by date and ranking is offered only on
  ``search``, where you read a top-20 list.
* Applying the source filter by joining ``msgs`` inside a ``count(*)`` cost
  1.28s per keystroke. Filters belong on the row query, never on a cost guard.

Nothing here caps row counts for display purposes. The TUI's DataTable
virtualises, so listing every session is fine; capping the browse list to the
newest N messages is what made older sessions unreachable.
"""

from __future__ import annotations

import re
import sqlite3

#: If the query uses any of these it is FTS5 syntax and is passed through as-is.
OPS = re.compile(r'"|\bAND\b|\bOR\b|\bNOT\b|\bNEAR\s*\(|\^|:')

ROLE_CYCLE = ("all", "user", "assistant")


def prep_query(q: str, prefix: bool = False) -> str:
    """Turn user input into a valid FTS5 MATCH expression.

    Quoting is not cosmetic: bare ``haproxy.cfg`` or ``kube-proxy`` is a *syntax
    error* in FTS5, not a zero-result query.

    ``prefix=True`` appends ``*`` to every token, which is what any live search
    box needs - a half-typed word is a prefix. ``"mik"`` matches nothing while
    ``"mik"*`` matches ``mikrotik``, and exact matching in a picker returns zero
    results until you finish the word.
    """
    q = q or ""
    if OPS.search(q):
        return q
    toks = []
    for tok in q.split():
        star = "*" if (prefix or tok.endswith("*")) else ""
        core = tok[:-1] if tok.endswith("*") else tok
        if not core:
            continue
        toks.append('"' + core.replace('"', '""') + '"' + star)
    return " ".join(toks)


def _limit_clause(limit: int | None) -> tuple[str, list]:
    return (" LIMIT ?", [limit]) if limit else ("", [])


class BadQuery(Exception):
    """The user's text is not a valid FTS5 expression."""


# ------------------------------------------------------------------- searching

def search(
    db: sqlite3.Connection,
    query: str,
    *,
    limit: int | None = 20,
    exact: bool = False,
    sort: str = "new",
    role: str | None = None,
    source: str | None = None,
    project: str | None = None,
    session: str | None = None,
    since: str | None = None,
    until: str | None = None,
    snippet: bool = True,
) -> list[dict]:
    where = ["msgs_fts MATCH ?"]
    params: list = [prep_query(query, prefix=not exact)]
    if source in ("claude", "codex"):
        where.append("m.source=?"); params.append(source)
    if role and role != "all":
        # LIKE, so Claude sidechain turns (user/sub) still count as user turns
        where.append("m.role LIKE ?"); params.append(f"%{role}%")
    if project:
        where.append("m.project LIKE ?"); params.append(f"%{project}%")
    if session:
        where.append("m.session LIKE ?"); params.append(f"%{session}%")
    if since:
        where.append("m.ts >= ?"); params.append(since)
    if until:
        where.append("m.ts <= ?"); params.append(until)

    order = {
        "rank": "bm25(msgs_fts), m.ts DESC",
        "new": "m.ts DESC",
        "old": "m.ts ASC",
    }[sort]
    body = (
        "snippet(msgs_fts,0,'{HL}','{OFF}','…',16)" if snippet
        else "substr(m.text,1,300)"
    )
    lim, lparams = _limit_clause(limit)
    sql = (
        f"SELECT m.id,m.source,m.session,m.project,m.role,m.ts,m.path,m.line,"
        f"{body},bm25(msgs_fts) "
        f"FROM msgs_fts JOIN msgs m ON m.id=msgs_fts.rowid "
        f"WHERE {' AND '.join(where)} ORDER BY {order}{lim}"
    )
    try:
        rows = db.execute(sql, params + lparams).fetchall()
    except sqlite3.OperationalError as e:
        raise BadQuery(str(e)) from e
    cols = ("id", "source", "session", "project", "role", "ts", "path", "line",
            "snippet", "score")
    return [dict(zip(cols, r)) for r in rows]


# -------------------------------------------------------------------- listings

def project_rows(
    db: sqlite3.Connection,
    *,
    source: str | None = None,
    sort: str = "new",
    contains: str | None = None,
    exact: str | None = None,
    limit: int | None = None,
) -> list[dict]:
    where, params = ["coalesce(project,'') <> ''"], []
    if source in ("claude", "codex"):
        where.append("source=?"); params.append(source)
    if exact:
        where.append("project=?"); params.append(exact)
    elif contains:
        where.append("project LIKE ?"); params.append(f"%{contains}%")
    direction = "ASC" if sort == "old" else "DESC"
    lim, lparams = _limit_clause(limit)
    rows = db.execute(
        f"SELECT project, count(DISTINCT session), count(*), max(ts), "
        f"group_concat(DISTINCT source) FROM msgs WHERE {' AND '.join(where)} "
        f"GROUP BY project ORDER BY max(ts) {direction}{lim}",
        params + lparams,
    ).fetchall()
    return [
        {"project": p, "sessions": ns, "messages": nm, "last": last,
         "sources": srcs or ""}
        for p, ns, nm, last, srcs in rows
    ]


def clean_title(t: str | None) -> str:
    """Reject text that is a wrapper rather than something the user typed."""
    t = re.sub(r"\s+", " ", t or "").strip()
    for bad in ("<", "Caveat:", "[tool:", "[result]"):
        if t.startswith(bad):
            return ""
    return t


def session_rows(
    db: sqlite3.Connection,
    *,
    limit: int | None = 40,
    project: str | None = None,
    project_exact: bool = False,
    source: str | None = None,
    contains: str | None = None,
    sort: str = "new",
) -> list[dict]:
    """Sessions with titles, newest last-activity first.

    ``project`` is a substring filter, which is what a user typing part of a path
    wants. ``project_exact=True`` matches one folder and one folder only - what
    descending into a folder means, and the difference between `/work/api` and
    `/work/api-old`.

    Titles come from **one** windowed query, not one query per session. The N+1
    version was invisible at 25 rows and would have been 477 round-trips at real
    scale.
    """
    where, params = [], []
    if project and project_exact:
        where.append("m.project=?"); params.append(project)
    elif project:
        where.append("m.project LIKE ?"); params.append(f"%{project}%")
    if source in ("claude", "codex"):
        where.append("m.source=?"); params.append(source)
    if contains:
        where.append(
            "(m.session LIKE ? OR m.path LIKE ? OR m.project LIKE ? "
            "OR coalesce(l.name,'') LIKE ? OR coalesce(f.cli_name,'') LIKE ?)"
        )
        params += [f"%{contains}%"] * 5
    order = ("max(m.ts) ASC, max(m.id) ASC" if sort == "old"
             else "max(m.ts) DESC, max(m.id) DESC")
    lim, lparams = _limit_clause(limit)
    sql = (
        "SELECT m.source, m.session, m.project, "
        # The session's own transcript, not just any file it has rows in: a Claude
        # session's subagent turns live in `<id>/subagents/*.jsonl` beside it, and
        # picking one of those loses the directory the session started in - which
        # is the only thing `claude --resume` will look in.
        "coalesce(max(CASE WHEN m.path LIKE '%'||m.session||'.jsonl' THEN m.path END), "
        "         max(m.path)), "
        "min(m.ts), max(m.ts), "
        "count(*), max(l.name), min(m.id), max(f.cli_name) "
        "FROM msgs m LEFT JOIN labels l ON l.session=m.session "
        "LEFT JOIN files f ON f.path=m.path "
        + (f"WHERE {' AND '.join(where)} " if where else "")
        + f"GROUP BY m.session ORDER BY {order}{lim}"
    )
    base = db.execute(sql, params + lparams).fetchall()
    if not base:
        return []

    titles = _titles(db, [r[1] for r in base])
    out = []
    for src, sess, proj, path, lo, hi, n, label, first_id, cli_name in base:
        out.append({
            "source": src, "session": sess, "project": proj, "path": path,
            "first": lo, "last": hi, "n": n, "label": label,
            "cli_name": cli_name, "first_id": first_id,
            "title": label or cli_name or titles.get(sess) or "(no user text)",
        })
    return out


#: above this many sessions it is cheaper to compute every title in one pass
#: than to bind one parameter per session (and safer than the SQLite variable
#: limit).
_TITLE_BIND_MAX = 400


def _titles(db: sqlite3.Connection, sessions: list[str]) -> dict[str, str]:
    """First user message per session that is not a wrapper, in one query."""
    want = set(sessions)
    if len(want) <= _TITLE_BIND_MAX:
        marks = ",".join("?" * len(want))
        cond, params = f"AND session IN ({marks})", list(want)
    else:
        cond, params = "", []
    titles: dict[str, str] = {}
    for sess, text in db.execute(
        "SELECT session, text FROM (SELECT session, text, "
        "row_number() OVER (PARTITION BY session ORDER BY id) rn FROM msgs "
        f"WHERE role LIKE '%user%' {cond}) WHERE rn <= 6 ORDER BY session, rn",
        params,
    ):
        if sess in titles or sess not in want:
            continue
        t = clean_title(text)
        if t:
            titles[sess] = t
    return titles


# ----------------------------------------------------------------- picker feed

#: How many matching messages one keystroke fetches.
#:
#: This is a *query cost* bound, not a display bound, and the difference matters.
#: Measured on a 20.7k-message corpus, a one-letter prefix query matches 14k rows:
#: fetching all of them costs ~520ms, fetching 500 costs ~20ms, and the ordering
#: has to touch every match either way. The browser's status line reports the
#: truncation, so it is never silent.
#:
#: Session and project listings are *not* capped - DataTable virtualises, and
#: "only the recent sessions are reachable" was the actual bug.
FEED_LIMIT = 500

#: How much of each message body one keystroke pulls back, for the excerpt.
#: `snippet()` is a joy in a top-20 CLI listing and a disaster at 500 rows: it
#: tripled the cost of the query above, all by itself.
EXCERPT_CHARS = 1000

_WS = re.compile(r"\s+")


def query_terms(q: str) -> list[str]:
    """The words a user typed, for highlighting. Not a parser - a best effort."""
    return [t for t in re.findall(r"[^\s\"'()^*:]+", q or "")
            if t.upper() not in ("AND", "OR", "NOT", "NEAR")]


def excerpt(text: str, terms: list[str], width: int = 160) -> tuple[str, int]:
    """A one-line excerpt around the first matching term.

    Returns ``(text, offset_of_match_in_excerpt)`` - the offset is -1 when no
    term was found within what was fetched, in which case the head is shown.
    """
    flat = _WS.sub(" ", text or "").strip()
    low = flat.lower()
    at = -1
    for t in terms:
        i = low.find(t.lower())
        if i >= 0 and (at < 0 or i < at):
            at = i
    if at < 0:
        return flat[:width], -1
    start = max(0, at - width // 3)
    piece = flat[start:start + width]
    if start:
        piece = "…" + piece
    return piece, at - start + (1 if start else 0)


def message_feed(
    db: sqlite3.Connection,
    q: str,
    *,
    source: str = "all",
    role: str = "all",
    project: str = "",
    project_exact: bool = False,
    sort: str = "new",
    limit: int | None = FEED_LIMIT,
) -> list[dict]:
    """Rows for the interactive message view. FTS5 is the matcher, always.

    An empty query matches nothing here on purpose: with no query the browser
    lists *sessions*, which is `session_rows`' job. Listing the newest messages
    instead only ever reached the last few days of a 20k-message corpus.
    """
    if not (q or "").strip():
        return []
    where = ["msgs_fts MATCH ?"]
    params: list = [prep_query(q, prefix=True)]
    if source in ("claude", "codex"):
        where.append("m.source=?"); params.append(source)
    if role in ("user", "assistant"):
        where.append("m.role LIKE ?"); params.append(f"%{role}%")
    if project and project_exact:
        where.append("m.project=?"); params.append(project)
    elif project:
        where.append("m.project LIKE ?"); params.append(f"%{project}%")
    direction = "ASC" if sort == "old" else "DESC"
    lim, lparams = _limit_clause(limit)
    sql = (
        f"SELECT m.id,m.source,m.session,m.project,m.role,m.ts,m.path,m.line,"
        f"substr(m.text,1,{EXCERPT_CHARS}) "
        f"FROM msgs_fts JOIN msgs m ON m.id=msgs_fts.rowid "
        f"WHERE {' AND '.join(where)} ORDER BY m.ts {direction}{lim}"
    )
    try:
        rows = db.execute(sql, params + lparams).fetchall()
    except sqlite3.OperationalError as e:
        raise BadQuery(str(e)) from e

    terms = query_terms(q)
    out = []
    for mid, src, sess, proj, role_, ts, path, line, body in rows:
        piece, at = excerpt(body, terms)
        out.append({"id": mid, "source": src, "session": sess, "project": proj,
                    "role": role_, "ts": ts, "path": path, "line": line,
                    "excerpt": piece, "match_at": at})
    return out


def sessions_named(
    db: sqlite3.Connection,
    text: str,
    *,
    source: str | None = None,
    project: str | None = None,
    project_exact: bool = False,
    sort: str = "new",
    limit: int | None = None,
) -> list[dict]:
    """Sessions whose name, label, id or path contains ``text``.

    Names are not message text, so FTS5 cannot find them: a session called
    `thecultt-data-fixes` is invisible to a search for those words unless someone
    happened to type them into the conversation.

    This is the other half of searching and it runs *alongside* the text search,
    never instead of it. As a fallback it was worse than useless: the moment any
    conversation mentions the name - which is exactly what happens when you paste
    "run codex resume, then select thecultt-data-fixes" anywhere - the text search
    returns hits, the fallback stays quiet, and the session that is actually
    *called* that is the one thing missing from the results.
    """
    text = (text or "").strip()
    if not text:
        return []
    return session_rows(db, limit=limit, contains=text, source=source,
                        project=project, project_exact=project_exact, sort=sort)


def match_count(db: sqlite3.Connection, q: str) -> int:
    """How many messages match, counted **in the FTS index alone**.

    Joining ``msgs`` here to apply the source filter is what once cost 1.28s per
    keystroke, so this is deliberately unfiltered and only used to tell the user
    that a list was truncated.
    """
    if not (q or "").strip():
        return 0
    try:
        return db.execute(
            "SELECT count(*) FROM msgs_fts WHERE msgs_fts MATCH ?",
            (prep_query(q, prefix=True),),
        ).fetchone()[0]
    except sqlite3.OperationalError as e:
        raise BadQuery(str(e)) from e
