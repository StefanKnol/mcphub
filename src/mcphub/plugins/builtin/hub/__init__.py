"""The hub's own backend: its documentation, and tools for managing it.

Built in rather than deployed. Every other backend is something an administrator
added and can remove; this one is part of the hub, so the hub creates it, keeps
its name reserved, and refuses to delete it. It can be disabled — an
administrator who does not want it exposed should be able to say so — but
disabling is reversible and deleting is not.

It is still an ordinary mount in every other respect. Same OAuth, same grants,
same levels, same endpoint shape. Making it a special case at the root would
have meant a second set of answers to every question this hub already answers.

`mcphub` is the reserved name. Specific enough to squat without taking a word
someone might want for a real backend, and it is where anything else about the
hub itself belongs.
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer

from ...base import BackendInstance, CheckResult, PluginDefaults
from . import docs, manage
from .docs import DOCS_DIR, ORDER, Topic, topics  # re-exported: the docs are also tested directly

SLUG = "mcphub"
"""Reserved: no other backend may take this name, and this one keeps it."""

PLUGIN_ID = "mcphub"
TITLE = "mcphub"

__all__ = ["SLUG", "PLUGIN_ID", "TITLE", "HubPlugin", "DOCS_DIR", "ORDER", "Topic", "topics"]


class HubPlugin(PluginDefaults):
    """One per hub, constructed by the hub it manages.

    Not loaded from an entry point like every other plugin, because it needs the
    hub itself and a module-level singleton would bind to whichever hub happened
    to start last — which is wrong in one process running two, and tests run two.
    """

    id = PLUGIN_ID
    name = "mcphub"
    description = (
        "This hub itself: its documentation for building apps, and tools for "
        "deploying them. Built in — it cannot be added or removed."
    )
    fields = ()

    def __init__(self, hub: Any) -> None:
        self._hub = hub

    def build(self, instance: BackendInstance) -> MCPServer:
        server = MCPServer(instance.title)
        docs.install(server)
        manage.install(server, self._hub)
        return server

    async def check(self, instance: BackendInstance) -> CheckResult:
        pages = topics()
        if not pages:
            return CheckResult(False, f"No documentation found at {DOCS_DIR}.")
        return CheckResult(True, f"{len(pages)} pages, and tools for managing this hub.")
