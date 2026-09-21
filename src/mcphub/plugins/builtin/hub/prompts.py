"""Ready-made prompts for the work this hub asks of an app.

Documentation answers "how does this work". A prompt answers "do this to my
project", which is a different thing to hand someone — and the thing they
actually want when they have an app in front of them and a hub to put it on.

It is deliberately short and mostly pointers. The pages are right there over
the same connection, and a prompt that restated them would be a second copy to
keep in step with the code.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).resolve().parents[3] / "data" / "prompts"

NAME = "make_it_an_mcphub_app"
TITLE = "Make an app work on this hub"
DESCRIPTION = (
    "A brief for an assistant working in an app's own repository: add an MCP "
    "server, annotate its tools so levels work, use the hub's storage, accounts "
    "and grants, and ship it. Mostly pointers into this hub's own documentation."
)


@lru_cache(maxsize=None)
def _template(name: str) -> str:
    path = PROMPTS_DIR / f"{name}.md"
    if not path.is_file():
        log.warning("prompt %s is missing from %s", name, PROMPTS_DIR)
        return ""
    return path.read_text(encoding="utf-8")


def integration_prompt(hub_url: str, app: str = "") -> str:
    """The brief, addressed to a particular hub and optionally a particular app."""
    text = _template("mcphub_app").replace("{hub}", hub_url.rstrip("/"))
    if not text:
        return "This hub has no integration prompt installed."
    if app.strip():
        # Ahead of the instructions rather than woven into them: what to work
        # on is the one thing the person knows and the template does not.
        text = f"The project to do this to: {app.strip()}\n\n{text}"
    return text


def install(server: Any, hub: Any) -> None:
    @server.prompt(name=NAME, title=TITLE, description=DESCRIPTION)
    def make_it_an_mcphub_app(app: str = "") -> str:
        """Turn a project into an app this hub can run.

        `app` optionally names what to work on — a repository, a directory, a
        sentence about it. Leave it out when the assistant is already there.
        """
        return integration_prompt(hub.settings.public_url, app)
