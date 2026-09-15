"""Plugin discovery."""

from __future__ import annotations

import logging
from importlib.metadata import entry_points

from .base import Plugin, validate_plugin

log = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "mcphub.plugins"


class PluginRegistry:
    def __init__(self) -> None:
        self._plugins: dict[str, Plugin] = {}

    def load_entry_points(self) -> None:
        """Load every plugin advertised on the ``mcphub.plugins`` group.

        A plugin that fails to import is logged and skipped rather than taking
        the hub down with it — one broken third-party backend should not stop
        you from reaching the others, which is exactly when you need the UI.
        """
        for ep in entry_points(group=ENTRY_POINT_GROUP):
            try:
                self.register(validate_plugin(ep.load()))
            except Exception:
                log.exception("plugin %r failed to load and was skipped", ep.name)

    def register(self, plugin: Plugin) -> None:
        if plugin.id in self._plugins:
            raise ValueError(f"duplicate plugin id {plugin.id!r}")
        self._plugins[plugin.id] = plugin
        log.info("registered plugin %s (%s)", plugin.id, plugin.name)

    def get(self, plugin_id: str) -> Plugin | None:
        return self._plugins.get(plugin_id)

    def all(self) -> list[Plugin]:
        return sorted(self._plugins.values(), key=lambda p: p.name)

    def __len__(self) -> int:
        return len(self._plugins)
