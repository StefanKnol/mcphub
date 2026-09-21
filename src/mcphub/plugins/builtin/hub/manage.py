"""Managing the hub itself, over MCP.

Deploying an app is a sequence of small decisions — find the server, name it,
fill in what it asks for, look at the tools, enable it — and every one of them
is already in the web UI. The point of having them here too is that the thing
doing the deploying is often the same thing that just wrote the app, and asking
it to describe the clicks for a person to perform is a worse loop than letting
it do them and say what it did.

Two checks apply to every call, and they are not the same check:

- **Hub rights.** Whether the account may configure backends at all, exactly as
  the settings pages ask. An account without them can read and nothing more.
- **The level** on this backend, enforced by the hub from the annotations
  below. `viewer` reads, `user` deploys and enables, `admin` removes.

Both, because they answer different questions: one is about the account, the
other about what this connector was granted. Neither substitutes for the other.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations

from .... import registry as mcp_registry
from .... import storage
from ....plugins.base import BackendInstance

log = logging.getLogger(__name__)

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                            idempotentHint=True, openWorldHint=False)
WRITES = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
DESTROYS = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False)

MAX_RESULTS = 20


def _caller(hub: Any) -> Any:
    """The account this request is being made by, as a users row."""
    token = get_access_token()
    subject = getattr(token, "subject", None)
    if not subject:
        raise ToolError("This request carried no account, so nothing can be decided about it.")
    row = hub.db.one("SELECT * FROM users WHERE username = ?", (subject,))
    if row is None:
        raise ToolError(f"The account {subject!r} no longer exists.")
    return row


def _may_manage(hub: Any) -> Any:
    row = _caller(hub)
    if not (row["is_admin"] or row["can_add_backends"]):
        raise ToolError(
            f"The account {row['username']!r} may not configure backends on this hub. "
            "An administrator grants that under Accounts."
        )
    return row


def _visible(hub: Any, row: Any) -> set[str] | None:
    """Slugs this account may see, or None meaning all of them.

    An account that may configure backends sees them all, because it can
    already open any backend's settings page — which shows strictly more than
    `describe_backend` does. Anyone else sees what they were granted.
    """
    if row["is_admin"] or row["can_add_backends"]:
        return None
    return {r["slug"] for r in hub.db.query(
        "SELECT b.slug FROM backend_grants g JOIN backends b ON b.id = g.backend_id "
        "WHERE g.user_id = ?", (row["id"],))}


def _slug_error(hub, slug: str) -> str | None:
    from ....web.routes import RESERVED_SLUGS, SLUG_RE

    if not SLUG_RE.match(slug):
        return (f"{slug!r} cannot be a URL name: lowercase letters, digits and "
                "dashes, 2 to 40 characters.")
    if slug in RESERVED_SLUGS:
        return f"{slug!r} is reserved by the hub. Pick another URL name."
    if hub.backend_row(slug) is not None:
        return f"A backend called {slug!r} already exists."
    return None


def install(server: Any, hub: Any) -> None:
    """Add the hub-management tools to `server`, acting on `hub`."""

    @server.tool(annotations=READ_ONLY)
    def list_backends() -> str:
        """Every backend on this hub that the calling account may see.

        An administrator sees all of them; anyone else sees what they have been
        granted. Each line is the URL name, title, whether it is enabled and
        live, and its MCP endpoint.
        """
        caller = _caller(hub)
        allowed = _visible(hub, caller)
        mounted = {m.slug for m in hub.mounts.active()} if hub.mounts else set()
        lines = []
        for row in hub.backend_rows():
            if allowed is not None and row["slug"] not in allowed:
                continue
            state = ("live" if row["slug"] in mounted else "enabled, not mounted") \
                if row["enabled"] else "disabled"
            lines.append(f"{row['slug']} — {row['title']} [{row['plugin_id']}] "
                         f"({state}) {hub.settings.public_url}/mcp/{row['slug']}")
        return "\n".join(lines) or "No backends this account can see."

    @server.tool(annotations=READ_ONLY)
    def describe_backend(slug: str) -> str:
        """Everything about one backend except its secrets.

        Its plugin, its settings, where its storage is, and which accounts have
        been granted it at which level.
        """
        caller = _caller(hub)
        allowed = _visible(hub, caller)
        if allowed is not None and slug not in allowed:
            raise ToolError(f"This account has not been granted {slug!r}.")
        row = hub.backend_row(slug)
        if row is None:
            raise ToolError(f"No backend called {slug!r}.")
        instance = hub.instance_from_row(row)
        grants = hub.db.query(
            "SELECT u.username, g.role FROM backend_grants g JOIN users u ON u.id = g.user_id "
            "WHERE g.backend_id = ? ORDER BY u.username", (row["id"],))
        return json.dumps({
            "slug": row["slug"],
            "title": row["title"],
            "plugin": row["plugin_id"],
            "enabled": bool(row["enabled"]),
            "endpoint": f"{hub.settings.public_url}/mcp/{row['slug']}",
            "storage": str(instance.storage) if instance.storage else None,
            # Values only; anything stored as a secret is not here, and naming
            # the keys is what lets a caller see that something *is* set.
            "settings": {k: v for k, v in instance.config.items()
                         if not k.startswith("registry_") and k not in ("tool_catalog",
                                                                        "resource_catalog",
                                                                        "prompt_catalog",
                                                                        "version_catalogs")},
            "secrets_set": sorted(instance.secrets),
            "granted_to": [{"account": g["username"], "level": g["role"]} for g in grants],
        }, indent=2)

    @server.tool(annotations=READ_ONLY)
    async def search_registry(query: str) -> str:
        """Search the official MCP registry for a server to deploy.

        Returns names to pass to `deploy_app`, with what each one is and which
        environment variables it declares.
        """
        _may_manage(hub)
        try:
            found = await mcp_registry.search(query, limit=MAX_RESULTS)
        except mcp_registry.RegistryError as exc:
            raise ToolError(f"The registry could not be reached: {exc}") from exc
        if not found:
            return f"Nothing in the registry matches {query!r}."
        return json.dumps([{
            "name": s.name,
            "title": s.title,
            "description": s.description,
            "suggested_slug": s.slug_hint,
            "launched": bool(s.command),
            "environment": [{"name": v.name, "required": v.required,
                             "description": v.description} for v in s.env],
        } for s in found], indent=2)

    @server.tool(annotations=WRITES)
    async def deploy_app(slug: str, registry_name: str = "", url: str = "",
                         title: str = "", environment: dict[str, str] | None = None) -> str:
        """Add a backend to this hub, from the registry or from a URL.

        Give either `registry_name` (a name from `search_registry`, which the
        hub will launch) or `url` (a streamable-HTTP MCP endpoint you are
        already running, including its path).

        `environment` fills the variables the server declares. Every value is
        stored encrypted, whether or not the server called it a secret.

        The backend is created **disabled**: its tools come from outside this
        hub, and they should be looked at before they attach to an account.
        Use `describe_backend` to see what arrived, then `set_backend_enabled`.
        """
        _may_manage(hub)
        slug = (slug or "").strip().lower()
        problem = _slug_error(hub, slug)
        if problem:
            raise ToolError(problem)
        if bool(registry_name) == bool(url):
            raise ToolError("Give exactly one of `registry_name` or `url`.")

        plugin = hub.registry.get("mcp-proxy")
        if plugin is None:
            raise ToolError("The proxy plugin is not installed, so nothing can be wrapped.")

        if registry_name:
            config, secrets, chosen_title = await _from_registry(registry_name,
                                                                 environment or {})
        else:
            config, secrets, chosen_title = _from_url(url, environment or {})

        instance = BackendInstance(slug=slug, title=title or chosen_title,
                                   plugin_id=plugin.id, config=config, secrets=secrets,
                                   storage=storage.path_for(hub.settings.data_dir, slug))
        try:
            config.update(await plugin.on_save(instance) or {})
        except Exception:  # noqa: BLE001 - the backend is still worth creating
            log.exception("could not introspect %s while deploying it", slug)

        from ....web.routes import _save_backend

        _save_backend(hub, slug=slug, plugin_id=plugin.id, title=instance.title,
                      enabled=False, config=config, secrets=secrets)
        tools = sorted(plugin.tool_names(instance))
        log.info("backend %s deployed over MCP", slug)
        return json.dumps({
            "slug": slug,
            "title": instance.title,
            "enabled": False,
            "storage": str(instance.storage),
            "tools_found": tools,
            "next": f"Review the tools, then set_backend_enabled({slug!r}, true). "
                    f"Its endpoint will be {hub.settings.public_url}/mcp/{slug}.",
        }, indent=2)

    @server.tool(annotations=WRITES)
    async def set_backend_enabled(slug: str, enabled: bool) -> str:
        """Bring a backend's endpoint up or take it down.

        Disabling leaves the configuration alone; it is how you stop a backend
        without losing what it took to set up.
        """
        _may_manage(hub)
        row = hub.backend_row(slug)
        if row is None:
            raise ToolError(f"No backend called {slug!r}.")
        hub.db.execute("UPDATE backends SET enabled = ? WHERE id = ?",
                       (int(enabled), row["id"]))
        if not enabled:
            await hub.mounts.unmount(slug)
            return f"{slug} is disabled. Its endpoint is down; its settings are kept."
        error = await hub.remount(slug)
        if error:
            raise ToolError(f"{slug} is enabled but did not start: {error}")
        return f"{slug} is live at {hub.settings.public_url}/mcp/{slug}."

    @server.tool(annotations=DESTROYS)
    async def remove_backend(slug: str) -> str:
        """Delete a backend, its credentials and its grants.

        Its stored files are **not** deleted: unmounting is reversible and a
        dropped database is not. `describe_backend` names the directory if you
        want to remove it yourself.
        """
        _may_manage(hub)
        row = hub.backend_row(slug)
        if row is None:
            raise ToolError(f"No backend called {slug!r}.")
        if slug == RESERVED:
            raise ToolError(f"{slug!r} is this hub's own backend and cannot be removed. "
                            "Disable it instead if you do not want it exposed.")

        from ....web.routes import _revoke_backend_credentials

        instance = hub.instance_from_row(row)
        plugin = hub.registry.get(row["plugin_id"])
        if plugin is not None:
            try:
                await plugin.on_delete(instance)
            except Exception as exc:  # noqa: BLE001 - reported, not swallowed
                raise ToolError(f"{slug} was not removed: its plugin could not release "
                                f"what it had provisioned: {exc}") from exc
        await hub.mounts.unmount(slug)
        _revoke_backend_credentials(hub, slug)
        hub.db.execute("DELETE FROM backends WHERE id = ?", (row["id"],))
        log.info("backend %s removed over MCP", slug)
        kept = instance.storage
        return (f"{slug} is gone: endpoint down, credentials revoked, grants dropped."
                + (f" Its files are still at {kept}." if kept else ""))


RESERVED = "mcphub"
"""This hub's own backend. Named here so `remove_backend` can refuse it."""


async def _from_registry(name: str, values: dict[str, str]) -> tuple[dict, dict, str]:
    try:
        found = await mcp_registry.get(name)
    except mcp_registry.RegistryError as exc:
        raise ToolError(f"The registry could not be reached: {exc}") from exc
    if found is None:
        raise ToolError(f"{name!r} is not in the registry. Try `search_registry` first.")

    missing = [v.name for v in found.env if v.required and not values.get(v.name)]
    if missing:
        raise ToolError(f"{name} requires {', '.join(missing)}. "
                        "Pass them in `environment`.")

    config: dict[str, Any] = {"registry_name": found.name, "timeout": 60, "verify_tls": True}
    if found.command:
        config.update({"command": found.command, "url": "", "connection": "launch"})
    else:
        config.update({"command": "", "url": found.remote_url, "connection": "url"})
    if found.icons:
        config["registry_icons"] = list(found.icons)
    if found.package:
        config["registry_package"] = {
            "registryType": found.package.registry_type,
            "identifier": found.package.identifier,
            "runtime": found.package.runtime,
            "args": list(found.package.args),
        }
    config["registry_env"] = [
        {"name": v.name, "description": v.description,
         "isRequired": v.required, "isSecret": v.secret} for v in found.env
    ]
    # Every declared variable is encrypted, not just the ones flagged secret:
    # which of them are sensitive is the server's claim, and a wrong claim
    # should not put a token in a plaintext column.
    secrets = {f"env_{key}": value for key, value in values.items() if value}
    return config, secrets, found.title


def _from_url(url: str, values: dict[str, str]) -> tuple[dict, dict, str]:
    url = (url or "").strip()
    if not url.startswith(("http://", "https://")):
        raise ToolError("`url` must be an http or https address, including the MCP path.")
    config: dict[str, Any] = {"url": url, "command": "", "connection": "url",
                              "timeout": 60, "verify_tls": True}
    secrets = {f"env_{key}": value for key, value in values.items() if value}
    return config, secrets, url.split("//", 1)[-1].split("/", 1)[0]
