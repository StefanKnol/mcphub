"""The hub's own documentation, as tools and resources.

Documentation about building apps for mcphub is most useful to the thing that
is going to write them, which is generally not a person with a browser open.
Serving it over MCP means an assistant can read the page about tool annotations
at the moment it is writing tools, rather than being told about it afterwards.

The pages ship with the hub instead of being fetched, so they describe the build
you are running rather than whatever is on a website today.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from mcp_types import ToolAnnotations

log = logging.getLogger(__name__)

DOCS_DIR = Path(__file__).resolve().parents[3] / "data" / "docs"

ORDER = ("overview", "annotations", "levels", "web-interface", "storage",
         "app-access", "mcphub-backend", "plugin", "publishing", "deploying")
"""Reading order. Anything not named here follows, alphabetically — a page
added to the directory should appear without also having to be listed."""

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                            idempotentHint=True, openWorldHint=False)


@dataclass(frozen=True)
class Topic:
    name: str
    title: str
    summary: str
    body: str


def _read(path: Path) -> Topic:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    title = lines[0].lstrip("# ").strip() if lines else path.stem
    return Topic(name=path.stem, title=title, summary=_summary(lines[1:]), body=text)


def _summary(lines: list[str]) -> str:
    """The first sentence after the heading.

    Taken from the whole paragraph rather than the first line: the pages are
    hard-wrapped, so a line is usually a sentence cut in half, and a list of
    topics made of half-sentences is worse than useless.
    """
    paragraph: list[str] = []
    for line in lines:
        if line.strip():
            paragraph.append(line.strip())
        elif paragraph:
            break
    joined = " ".join(paragraph)
    head, stop, _ = joined.partition(". ")
    return (head + "." if stop else joined)[:200]


@lru_cache(maxsize=1)
def topics() -> tuple[Topic, ...]:
    if not DOCS_DIR.is_dir():
        log.warning("documentation directory %s is missing", DOCS_DIR)
        return ()
    found = {p.stem: _read(p) for p in sorted(DOCS_DIR.glob("*.md"))}
    ordered = [found.pop(name) for name in ORDER if name in found]
    return tuple(ordered + sorted(found.values(), key=lambda t: t.name))


def _find(name: str) -> Topic | None:
    wanted = (name or "").strip().lower().removesuffix(".md")
    return next((t for t in topics() if t.name == wanted), None)


def _matches(topic: Topic, query: str) -> list[str]:
    """The lines of one page that mention `query`, with a line either side.

    Whole paragraphs would be more readable and much longer; a search result is
    for deciding which page to read, and `read_topic` is right there.
    """
    lines = topic.body.splitlines()
    hits = [i for i, line in enumerate(lines) if query in line.lower()]
    out: list[str] = []
    for i in hits[:5]:
        window = [line for line in lines[max(0, i - 1): i + 2] if line.strip()]
        out.append(" ".join(window).strip())
    return out


def install(server: Any) -> None:
    """Add the documentation tools and one resource per page to `server`."""

    @server.tool(annotations=READ_ONLY)
    def list_topics() -> str:
        """Every documentation page, in reading order, with a one-line summary.

        Start here. `read_topic` then returns any of them in full.
        """
        if not topics():
            return "No documentation is installed with this hub."
        return "\n".join(f"{t.name} — {t.title}: {t.summary}" for t in topics())

    @server.tool(annotations=READ_ONLY)
    def read_topic(topic: str) -> str:
        """One documentation page in full, as Markdown.

        `topic` is a name from `list_topics`, such as "annotations".
        """
        found = _find(topic)
        if found is None:
            known = ", ".join(t.name for t in topics()) or "none"
            return f"No page named {topic!r}. Available: {known}."
        return found.body

    @server.tool(annotations=READ_ONLY)
    def search_docs(query: str) -> str:
        """Lines across every page that mention `query`, with the page to read.

        For finding which page covers something — `read_topic` gives the whole
        page once you know.
        """
        wanted = (query or "").strip().lower()
        if not wanted:
            return "Give something to search for."
        out: list[str] = []
        for topic in topics():
            for line in _matches(topic, wanted):
                out.append(f"[{topic.name}] {line}")
        if not out:
            return (f"Nothing mentions {query!r}. "
                    f"Pages: {', '.join(t.name for t in topics())}.")
        return "\n".join(out[:40])

    for topic in topics():
        _expose(server, topic)


def _expose(server: Any, topic: Topic) -> None:
    """Offer a page as a resource too, so a client can attach it directly.

    A function per page rather than one templated `docs://{name}` resource: a
    template is not listed, so a client offering a menu of what it can attach
    would show nothing at all.
    """
    body = topic.body

    @server.resource(f"docs://{topic.name}", name=topic.title,
                     description=topic.summary, mime_type="text/markdown")
    def page() -> str:
        return body
