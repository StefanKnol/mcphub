"""The configuration UI.

This is the replacement for a pile of environment variables and a YAML file:
adding a device, rotating its password or taking a backend offline happens
here, and takes effect immediately without restarting anything.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from urllib.parse import urlparse
from typing import Any

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route
from starlette.templating import Jinja2Templates

from ..crypto import hash_password, verify_password
from ..db import utcnow
from .. import registry as mcp_registry
from ..plugins.base import BackendInstance, ConfigField, choice_pairs
from .session import current_user, end_session, start_session

log = logging.getLogger(__name__)

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,38}[a-z0-9]$")

# Reserved because a backend mounted at one of these would shadow the hub's
# own routes and, in the case of the OAuth endpoints, break authentication
# for every other backend at the same time.
RESERVED_SLUGS = {"login", "logout", "account", "accounts", "backends", "healthz", "mcp",
                  "authorize", "token", "register", "registry", "revoke"}


def _initials(title: str) -> str:
    """Up to two letters, for a backend with no logo to show."""
    words = [w for w in re.split(r"[\s._-]+", title or "") if w and w[0].isalnum()]
    if not words:
        return "?"
    if len(words) == 1:
        return words[0][:2].upper()
    return (words[0][0] + words[1][0]).upper()


def _config_value(row: Any, key: str) -> str:
    try:
        return str(json.loads(row["config_json"]).get(key) or "")
    except (json.JSONDecodeError, TypeError, KeyError):
        return ""


def _backend_icon(row: Any) -> str | None:
    """A logo for the card, from the registry entry this backend came from.

    Only entries added from the registry have one, and only some of those
    publish icons, so this is genuinely optional and the template falls back
    to a monogram rather than a placeholder repeated down the page.
    """
    try:
        config = json.loads(row["config_json"])
    except (json.JSONDecodeError, TypeError, KeyError):
        return None
    icons = config.get("registry_icons") or []
    if isinstance(icons, list) and icons:
        first = icons[0]
        src = first.get("src") if isinstance(first, dict) else first
        # Remote images only; a data: or javascript: URL from a third-party
        # listing has no business being rendered in the admin page.
        if isinstance(src, str) and src.startswith(("https://", "http://")):
            return src
    return None


def _resource_slug(resource: str | None) -> str | None:
    """The backend a token is being requested for, from its RFC 8707 resource."""
    if not resource:
        return None
    path = urlparse(str(resource)).path.rstrip("/")
    marker = "/mcp/"
    return path[path.rindex(marker) + len(marker):] if marker in path else None


def _save_backend(hub: Any, *, slug: str, plugin_id: str, title: str, enabled: bool,
                  config: dict[str, Any], secrets: dict[str, Any], row: Any = None) -> None:
    blob = hub.secrets.seal(secrets) if secrets else None
    if row is None:
        hub.db.execute(
            "INSERT INTO backends (slug, plugin_id, title, enabled, config_json, secrets_blob, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (slug, plugin_id, title, int(enabled), json.dumps(config), blob, utcnow(), utcnow()),
        )
    else:
        hub.db.execute(
            "UPDATE backends SET slug = ?, title = ?, enabled = ?, config_json = ?, "
            "secrets_blob = ?, updated_at = ? WHERE id = ?",
            (slug, title, int(enabled), json.dumps(config), blob, utcnow(), row["id"]),
        )


def build(hub: Any) -> list[Route]:
    def render(request: Request, template: str, status_code: int = 200, **context: Any) -> Response:
        signed_in = current_user(hub.db, request)
        row = hub.db.one("SELECT is_admin FROM users WHERE id = ?", (signed_in["id"],)) if signed_in else None
        return TEMPLATES.TemplateResponse(
            request, template,
            {
                "user": signed_in,
                "public_url": hub.settings.public_url,
                # Every page's navigation needs it, so it is part of the base
                # context rather than something each handler remembers to pass.
                "is_admin": bool(row and row["is_admin"]),
                **context,
            },
            status_code=status_code,
        )

    def require_user(request: Request) -> dict[str, Any] | None:
        return current_user(hub.db, request)  # type: ignore[return-value]

    def account_row(user: dict[str, Any] | None) -> Any:
        """Named distinctly from the `account` route handler below.

        Both lived in this scope as `account`, and the handler defined later
        won, so the permission checks were calling a coroutine and every
        backend page returned a 500.
        """
        return hub.db.one("SELECT * FROM users WHERE id = ?", (user["id"],)) if user else None

    def may_manage_backends(user: dict[str, Any] | None) -> bool:
        """Backends are shared, so configuring one affects everyone granted it.

        Admins always may; `can_add_backends` promotes an ordinary account to
        the same, which is the trust this toggle represents. What it does not
        grant is managing other accounts.
        """
        row = account_row(user)
        return bool(row and (row["is_admin"] or row["can_add_backends"]))

    def is_admin(user: dict[str, Any] | None) -> bool:
        row = account_row(user)
        return bool(row and row["is_admin"])

    def granted_backends(user_id: int) -> set[str]:
        return {r["slug"] for r in hub.db.query(
            "SELECT b.slug FROM backend_grants g JOIN backends b ON b.id = g.backend_id "
            "WHERE g.user_id = ?", (user_id,))}

    def may_use(user: dict[str, Any], slug: str) -> bool:
        return is_admin(user) or slug in granted_backends(user["id"])

    def denied(request: Request, message: str) -> Response:
        return render(request, "error.html", message=message, status_code=403)

    def redirect_to_login(request: Request) -> Response:
        return RedirectResponse(f"/login?next={request.url.path}", status_code=303)

    # ── authentication ────────────────────────────────────────────────────

    async def login(request: Request) -> Response:
        """Serves both the UI sign-in and the OAuth consent step.

        `?req=` marks an OAuth authorization parked by the provider: after a
        successful sign-in the browser is sent on to the MCP client's redirect
        URI with a fresh authorization code, rather than to the dashboard.
        """
        auth_request = request.query_params.get("req")
        next_url = request.query_params.get("next", "/")
        pending = hub.provider.get_pending(auth_request) if auth_request else None
        client_name = None
        if auth_request:
            if pending is None:
                return render(request, "login.html", error=(
                    "That sign-in link has expired. Start the connection again from your MCP client."
                ), auth_request=None, next_url="/", client_name=None, status_code=400)
            client = await hub.provider.get_client(pending.client_id)
            client_name = (client.client_name if client else None) or pending.client_id

        if request.method == "GET":
            user = current_user(hub.db, request)
            # Already signed in and this is an OAuth hop: still show the
            # consent screen. Silently minting a token for whatever asked
            # would let any page start a flow the user never agreed to.
            if user and not auth_request:
                return RedirectResponse(next_url, status_code=303)
            return render(request, "login.html", error=None, auth_request=auth_request,
                          next_url=next_url, client_name=client_name)

        form = await request.form()
        username = str(form.get("username", "")).strip()
        password = str(form.get("password", ""))
        remember = form.get("remember") is not None
        user_id = hub.provider.authenticate_user(username, password)
        if user_id is None:
            return render(request, "login.html", error="Incorrect username or password.",
                          auth_request=auth_request, next_url=next_url,
                          client_name=client_name, status_code=401)

        if auth_request:
            # Refuse here rather than minting a token that every MCP request
            # would then reject: a connector that appears to authorise and then
            # fails on use is a much worse thing to debug than a refusal now.
            wanted = _resource_slug(pending.params.resource if pending else None)
            if wanted and not may_use({"id": user_id}, wanted):
                return render(request, "login.html", error=(
                    f"This account has not been granted access to the {wanted!r} backend. "
                    "Ask an administrator to grant it, then connect again."
                ), auth_request=None, next_url="/", client_name=client_name, status_code=403)

            redirect_url = hub.provider.complete_authorization(auth_request, user_id)
            response = RedirectResponse(redirect_url, status_code=303)
            start_session(hub.db, response, user_id, remember=remember,
                          secure=hub.settings.public_url.startswith("https"))
            return response

        response = RedirectResponse(next_url if next_url.startswith("/") else "/", status_code=303)
        start_session(hub.db, response, user_id, remember=remember,
                      secure=hub.settings.public_url.startswith("https"))
        return response

    async def logout(request: Request) -> Response:
        response = RedirectResponse("/login", status_code=303)
        end_session(hub.db, request, response)
        return response

    # ── dashboard ─────────────────────────────────────────────────────────

    async def dashboard(request: Request) -> Response:
        if not require_user(request):
            return redirect_to_login(request)
        user = current_user(hub.db, request)
        mounted = {m.slug for m in hub.mounts.active()} if hub.mounts else set()
        visible = None if is_admin(user) else granted_backends(user["id"])
        backends = [
            {
                "slug": row["slug"],
                "title": row["title"],
                "plugin_id": row["plugin_id"],
                "plugin_name": getattr(hub.registry.get(row["plugin_id"]), "name", row["plugin_id"]),
                "enabled": bool(row["enabled"]),
                "mounted": row["slug"] in mounted,
                "url": f"{hub.settings.public_url}/mcp/{row['slug']}",
                "updated_at": row["updated_at"],
                "icon": _backend_icon(row),
                "initials": _initials(row["title"]),
                "version": _config_value(row, "upstream_version"),
                "latest": _config_value(row, "latest_version"),
                "pinnable": bool(_config_value(row, "registry_name")
                                 and json.loads(row["config_json"]).get("registry_package")),
                "pinned": (lambda r: r["version"] if r else "")(hub.db.one(
                    "SELECT version FROM backend_pins WHERE user_id = ? AND backend_id = ?",
                    (user["id"], row["id"])) if user else None),
                "upstream_name": _config_value(row, "upstream_name"),
            }
            for row in hub.backend_rows()
            if visible is None or row["slug"] in visible
        ]
        return render(request, "dashboard.html", backends=backends,
                      plugins=hub.registry.all() if may_manage_backends(user) else [],
                      can_manage=may_manage_backends(user), is_admin=is_admin(user))

    # ── backend create / edit ─────────────────────────────────────────────

    async def form_values(plugin: Any, instance: BackendInstance | None) -> list[dict[str, Any]]:
        values = []
        for f in plugin.fields_for(instance):
            options: list[Any] = []
            selected: list[str] = []
            if f.secret:
                # Never send a stored secret back to the browser.
                value, has_value = "", bool(instance and f.key in instance.secrets)
            else:
                value = instance.config.get(f.key, f.default) if instance else f.default
                has_value = False

            if f.type == "multiselect":
                raw = value or []
                selected = list(raw) if isinstance(raw, list) else [v for v in str(raw).split(",") if v]
                if instance is not None:
                    # Fetched live: the choices belong to the upstream, not to us.
                    # A failure here leaves the list empty rather than breaking
                    # the page, and the saved selection is still shown.
                    options = list(await plugin.options(instance, f.key))
                value = ""

            values.append({
                "field": f, "value": "" if value is None else value, "has_value": has_value,
                "options": options, "selected": selected, "choices": choice_pairs(f),
            })
        return values

    def split_fields(plugin: Any, form: Any, existing: BackendInstance | None) -> tuple[dict, dict, list[str]]:
        config: dict[str, Any] = {}
        secret: dict[str, Any] = dict(existing.secrets) if existing else {}
        errors: list[str] = []

        for f in plugin.fields_for(existing):
            if f.type == "multiselect":
                config[f.key] = [str(v) for v in form.getlist(f.key)]
                continue
            raw = form.get(f.key)
            if f.type == "bool":
                config[f.key] = raw is not None
                continue
            text = str(raw or "").strip()
            if f.secret:
                if text:
                    secret[f.key] = text
                elif f.required and f.key not in secret:
                    errors.append(f"{f.label} is required.")
                continue
            if not text:
                if f.required and f.default is None:
                    errors.append(f"{f.label} is required.")
                config[f.key] = f.default if f.default is not None else ""
                continue
            if f.type == "number":
                try:
                    config[f.key] = float(text) if "." in text else int(text)
                except ValueError:
                    errors.append(f"{f.label} must be a number.")
                continue
            config[f.key] = text
        return config, secret, errors

    async def backend_form(request: Request) -> Response:
        user = require_user(request)
        if not user:
            return redirect_to_login(request)
        if not may_manage_backends(user):
            return denied(request, "This account cannot configure backends.")

        slug = request.path_params.get("slug")
        row = hub.backend_row(slug) if slug else None
        if slug and row is None:
            return render(request, "error.html", message=f"No backend named {slug!r}.", status_code=404)

        form = await request.form() if request.method == "POST" else None
        # On create, the plugin comes from the query string on GET and from a
        # hidden field on POST, so the choice survives a submit even if the
        # query string is dropped.
        plugin_id = (
            row["plugin_id"] if row
            else str((form or {}).get("plugin_id", "") or request.query_params.get("plugin", ""))
        )
        plugin = hub.registry.get(plugin_id)
        if plugin is None:
            return render(request, "error.html",
                          message=f"Plugin {plugin_id!r} is not installed.", status_code=404)

        instance = hub.instance_from_row(row) if row else None

        if request.method == "GET":
            return render(request, "backend_form.html", plugin=plugin, row=row,
                          fields=await form_values(plugin, instance), errors=[],
                          slug=slug or "", title=row["title"] if row else "",
                          # A plugin whose tool surface comes from elsewhere starts
                          # disabled, so its tools are reviewed before they attach.
                          enabled=bool(row["enabled"]) if row
                          else not getattr(plugin, "review_before_enable", False))

        assert form is not None
        new_slug = str(form.get("slug", "")).strip().lower()
        title = str(form.get("title", "")).strip()
        enabled = form.get("enabled") is not None
        posted, secret, errors = split_fields(plugin, form, instance)
        # Overlaid on what is already stored, not replacing it. The form covers
        # the plugin's declared fields; a backend also carries things no field
        # maps to — where it came from in the registry, its package reference,
        # the catalogues read from it — and building the config from the form
        # alone silently discarded all of that on the first save.
        config = {**(instance.config if instance else {}), **posted}

        if not SLUG_RE.match(new_slug):
            errors.append("URL name must be lowercase letters, digits and dashes (2–40 characters).")
        elif new_slug in RESERVED_SLUGS:
            errors.append(f"{new_slug!r} is reserved — pick another URL name.")
        elif new_slug != slug and hub.backend_row(new_slug) is not None:
            errors.append(f"A backend with the URL name {new_slug!r} already exists.")
        if not title:
            errors.append("Display name is required.")

        if errors:
            return render(request, "backend_form.html", plugin=plugin, row=row,
                          fields=await form_values(plugin, instance), errors=errors,
                          slug=new_slug, title=title, enabled=enabled, status_code=400)

        # Give the plugin a chance to cache what it discovered (an upstream tool
        # catalogue, say) so that `build` never needs the network. A failure here
        # must not lose the user's edits, so it is folded in and ignored.
        try:
            discovered = await plugin.on_save(
                BackendInstance(slug=new_slug, title=title, plugin_id=plugin.id,
                                config=config, secrets=secret)
            )
            config.update(discovered or {})
        except Exception:  # noqa: BLE001 - saving is the priority
            log.exception("on_save hook failed for backend %s", new_slug)

        _save_backend(hub, slug=new_slug, plugin_id=plugin.id, title=title, enabled=enabled,
                      config=config, secrets=secret, row=row)
        if row is not None and slug != new_slug:
            await hub.mounts.unmount(slug)

        error = await hub.remount(new_slug)
        if error:
            return render(request, "error.html",
                          message=f"Saved, but the backend could not be started: {error}", status_code=500)
        return RedirectResponse("/", status_code=303)

    async def backend_test(request: Request) -> Response:
        """Live connectivity check, called by the Test button."""
        user = require_user(request)
        if not user:
            return JSONResponse({"ok": False, "detail": "Not signed in."}, status_code=401)
        if not may_use(user, request.path_params["slug"]):
            return JSONResponse({"ok": False, "detail": "No access to this backend."}, status_code=403)
        row = hub.backend_row(request.path_params["slug"])
        if row is None:
            return JSONResponse({"ok": False, "detail": "No such backend."}, status_code=404)
        plugin = hub.registry.get(row["plugin_id"])
        if plugin is None:
            return JSONResponse({"ok": False, "detail": f"Plugin {row['plugin_id']!r} is not installed."})
        result = await plugin.check(hub.instance_from_row(row))
        return JSONResponse({"ok": result.ok, "detail": result.detail})

    async def backend_refresh(request: Request) -> Response:
        """Re-launch the backend and re-read what it offers.

        This is what "update" means for a launched server: `uvx` and `npx`
        resolve the package again each time they start, so remounting is what
        picks up a new release. The cached tool list is re-read at the same
        time, because otherwise a server can gain tools and this hub goes on
        serving the old list indefinitely.
        """
        user = require_user(request)
        if not user:
            return redirect_to_login(request)
        slug = request.path_params["slug"]
        if not may_manage_backends(user):
            return JSONResponse({"ok": False, "detail": "This account cannot configure backends."},
                                status_code=403)

        row = hub.backend_row(slug)
        if row is None:
            return JSONResponse({"ok": False, "detail": "No such backend."}, status_code=404)
        plugin = hub.registry.get(row["plugin_id"])
        if plugin is None:
            return JSONResponse({"ok": False, "detail": f"Plugin {row['plugin_id']!r} is not installed."},
                                status_code=400)

        before = hub.instance_from_row(row)
        was = getattr(plugin, "tool_names", lambda _i: set())(before)
        old_version = before.config.get("upstream_version") or ""

        try:
            discovered = await plugin.on_save(before) or {}
        except Exception as exc:  # noqa: BLE001 - reported to the caller
            log.exception("refresh failed for backend %s", slug)
            return JSONResponse({"ok": False, "detail": f"{type(exc).__name__}: {exc}"}, status_code=502)
        if not discovered:
            return JSONResponse({"ok": False,
                                 "detail": "Could not reach the server, so nothing was changed."},
                                status_code=502)

        config = {**before.config, **discovered}
        _save_backend(hub, slug=slug, plugin_id=row["plugin_id"], title=row["title"],
                      enabled=bool(row["enabled"]), config=config, secrets=before.secrets, row=row)

        after = hub.instance_from_row(hub.backend_row(slug))
        now = getattr(plugin, "tool_names", lambda _i: set())(after)
        new_version = config.get("upstream_version") or ""

        error = await hub.remount(slug)
        if error:
            return JSONResponse({"ok": False, "detail": f"Refreshed, but could not restart: {error}"},
                                status_code=500)

        added, removed = sorted(now - was), sorted(was - now)
        parts = []
        if new_version and new_version != old_version:
            parts.append(f"updated {old_version or '?'} -> {new_version}")
        elif new_version:
            parts.append(f"version {new_version}, unchanged")
        if added:
            parts.append(f"{len(added)} new tool(s): {', '.join(added[:4])}"
                         + ("..." if len(added) > 4 else ""))
        if removed:
            parts.append(f"{len(removed)} tool(s) gone: {', '.join(removed[:4])}"
                         + ("..." if len(removed) > 4 else ""))
        if not parts:
            parts.append(f"nothing changed ({len(now)} tools)")

        # Tools the upstream no longer has would otherwise sit in the allowlist
        # forever, and reappear if it ever brings them back.
        if removed and isinstance(config.get("tools"), list):
            kept = [t for t in config["tools"] if t in now]
            if kept != config["tools"]:
                config["tools"] = kept
                _save_backend(hub, slug=slug, plugin_id=row["plugin_id"], title=row["title"],
                              enabled=bool(row["enabled"]), config=config,
                              secrets=before.secrets, row=hub.backend_row(slug))
                await hub.remount(slug)

        return JSONResponse({"ok": True, "detail": "; ".join(parts)})

    async def backend_versions(request: Request) -> Response:
        """Versions this backend can be pinned to, for the selector."""
        user = require_user(request)
        slug = request.path_params["slug"]
        if not user or not may_use(user, slug):
            return JSONResponse({"versions": []}, status_code=403)

        row = hub.backend_row(slug)
        if row is None:
            return JSONResponse({"versions": []}, status_code=404)
        config = json.loads(row["config_json"])
        name = config.get("registry_name")
        if not name or not config.get("registry_package"):
            # A hand-written command has nothing reliable to pin against.
            return JSONResponse({"versions": [], "pinnable": False})

        try:
            available = await mcp_registry.versions(name)
        except mcp_registry.RegistryError as exc:
            return JSONResponse({"versions": [], "pinnable": True, "error": str(exc)})

        current = hub.db.one(
            "SELECT version FROM backend_pins WHERE user_id = ? AND backend_id = ?",
            (user["id"], row["id"]))
        return JSONResponse({
            "pinnable": True,
            "pinned": current["version"] if current else "",
            "default": config.get("upstream_version", ""),
            "versions": [{"version": v.version, "latest": v.is_latest} for v in available],
        })

    async def backend_pin(request: Request) -> Response:
        """Choose the version this account gets. Only this account.

        Pinning is not a permission: anyone granted the backend may hold
        themselves on an older version without affecting anyone else. The cost
        is that the hub then runs both, which is why the version is launched
        and read here rather than taken on trust.
        """
        user = require_user(request)
        slug = request.path_params["slug"]
        if not user:
            return JSONResponse({"ok": False, "detail": "Not signed in."}, status_code=401)
        if not may_use(user, slug):
            return JSONResponse({"ok": False, "detail": "No access to this backend."}, status_code=403)

        row = hub.backend_row(slug)
        if row is None:
            return JSONResponse({"ok": False, "detail": "No such backend."}, status_code=404)
        version = str((await request.form()).get("version", "")).strip()

        if not version:
            hub.db.execute("DELETE FROM backend_pins WHERE user_id = ? AND backend_id = ?",
                           (user["id"], row["id"]))
            return JSONResponse({"ok": True, "detail": "Following the default version."})

        plugin = hub.registry.get(row["plugin_id"])
        instance = hub.instance_from_row(row)
        if plugin is None or not instance.config.get("registry_package"):
            return JSONResponse({"ok": False,
                                 "detail": "This backend has no package reference, so it cannot be pinned."},
                                status_code=400)

        catalogs = dict(instance.config.get("version_catalogs") or {})
        if version not in catalogs:
            # Read it now rather than at mount time: building a backend must
            # stay offline, and a version that cannot start should fail here
            # where there is somewhere to say so.
            shaped = plugin.variant(instance, version)
            try:
                discovered = await plugin.on_save(shaped) or {}
            except Exception as exc:  # noqa: BLE001 - reported to the caller
                log.exception("could not read version %s of %s", version, slug)
                return JSONResponse({"ok": False, "detail": f"{type(exc).__name__}: {exc}"},
                                    status_code=502)
            if not discovered:
                return JSONResponse(
                    {"ok": False,
                     "detail": f"Version {version} could not be started, so it was not pinned."},
                    status_code=502)
            catalogs[version] = {
                "tools": discovered.get("tool_catalog", "[]"),
                "resources": discovered.get("resource_catalog", "[]"),
                "prompts": discovered.get("prompt_catalog", "[]"),
                "name": discovered.get("upstream_name", ""),
                "version": discovered.get("upstream_version", version),
            }
            config = {**instance.config, "version_catalogs": catalogs}
            _save_backend(hub, slug=slug, plugin_id=row["plugin_id"], title=row["title"],
                          enabled=bool(row["enabled"]), config=config,
                          secrets=instance.secrets, row=row)

        hub.db.execute(
            "INSERT INTO backend_pins (user_id, backend_id, version, created_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (user_id, backend_id) DO UPDATE SET version = excluded.version",
            (user["id"], row["id"], version, utcnow()))

        tools = len(json.loads(catalogs[version].get("tools") or "[]"))
        return JSONResponse({"ok": True,
                             "detail": f"Pinned to {version} ({tools} tools). Only this account is affected."})

    async def backend_delete(request: Request) -> Response:
        user = require_user(request)
        if not user:
            return redirect_to_login(request)
        if not may_manage_backends(user):
            return denied(request, "This account cannot configure backends.")
        slug = request.path_params["slug"]
        await hub.mounts.unmount(slug)
        hub.db.execute("DELETE FROM backends WHERE slug = ?", (slug,))
        return RedirectResponse("/", status_code=303)

    # ── account ───────────────────────────────────────────────────────────

    async def account(request: Request) -> Response:
        user = require_user(request)
        if not user:
            return redirect_to_login(request)
        if request.method == "GET":
            return render(request, "account.html", error=None, done=False)

        form = await request.form()
        current = str(form.get("current_password", ""))
        new = str(form.get("new_password", ""))
        row = hub.db.one("SELECT password_hash FROM users WHERE id = ?", (user["id"],))
        if not row or not verify_password(current, row["password_hash"]):
            return render(request, "account.html", error="Current password is incorrect.", done=False, status_code=401)
        if len(new) < 12:
            return render(request, "account.html", error="New password must be at least 12 characters.",
                          done=False, status_code=400)
        hub.db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(new), user["id"]))
        # Existing OAuth tokens survive on purpose: a password change should
        # not silently break every connector the user has already approved.
        hub.db.execute("DELETE FROM web_sessions WHERE user_id = ?", (user["id"],))
        response = render(request, "account.html", error=None, done=True)
        return response

    # ── registry ──────────────────────────────────────────────────────────

    async def registry_search(request: Request) -> Response:
        user = require_user(request)
        if not user:
            return redirect_to_login(request)
        if not may_manage_backends(user):
            return denied(request, "This account cannot add backends.")
        query = request.query_params.get("q", "").strip()
        results, error = [], None
        if query:
            try:
                results = await mcp_registry.search(query, limit=25)
            except mcp_registry.RegistryError as exc:
                error = str(exc)
        return render(request, "registry.html", query=query, results=results, error=error)

    async def registry_add(request: Request) -> Response:
        user = require_user(request)
        if not user:
            return redirect_to_login(request)
        if not may_manage_backends(user):
            return denied(request, "This account cannot add backends.")
        name = request.query_params.get("name", "") or (
            str((await request.form()).get("registry_name", "")) if request.method == "POST" else ""
        )
        try:
            server = await mcp_registry.get(name)
        except mcp_registry.RegistryError as exc:
            return render(request, "error.html", message=str(exc), status_code=502)
        if server is None:
            return render(request, "error.html", message=f"{name!r} is not in the registry.", status_code=404)

        if request.method == "GET":
            return render(request, "registry_add.html", server=server, errors=[],
                          slug=server.slug_hint, title=server.title, values={})

        form = await request.form()
        slug = str(form.get("slug", "")).strip().lower()
        title = str(form.get("title", "")).strip() or server.title
        values = {v.name: str(form.get(f"env_{v.name}", "")).strip() for v in server.env}

        errors: list[str] = []
        if not SLUG_RE.match(slug):
            errors.append("URL name must be lowercase letters, digits and dashes (2-40 characters).")
        elif slug in RESERVED_SLUGS:
            errors.append(f"{slug!r} is reserved - pick another URL name.")
        elif hub.backend_row(slug) is not None:
            errors.append(f"A backend with the URL name {slug!r} already exists.")
        for var in server.env:
            if var.required and not values.get(var.name):
                errors.append(f"{var.name} is required by this server.")
        if errors:
            return render(request, "registry_add.html", server=server, errors=errors,
                          slug=slug, title=title, values=values, status_code=400)

        plugin = hub.registry.get("mcp-proxy")
        if plugin is None:
            return render(request, "error.html", message="The proxy plugin is not installed.", status_code=500)

        config: dict[str, Any] = {"registry_name": server.name, "timeout": 60, "verify_tls": True}
        if server.command:
            config.update({"command": server.command, "url": "", "connection": "launch"})
        else:
            config.update({"command": "", "url": server.remote_url, "connection": "url"})

        # Keep the declaration, not just the values. It is what lets the settings
        # page go on naming these variables and describing them, instead of
        # collapsing to a freeform blob the moment the backend exists.
        if server.icons:
            config["registry_icons"] = list(server.icons)
        if server.package:
            # Kept so a pinned version can be expressed precisely. Guessing
            # which token of a command line is the package would eventually
            # rewrite the wrong one.
            config["registry_package"] = {
                "registryType": server.package.registry_type,
                "identifier": server.package.identifier,
                "runtime": server.package.runtime,
                "args": list(server.package.args),
            }
        config["registry_env"] = [
            {"name": v.name, "description": v.description,
             "isRequired": v.required, "isSecret": v.secret}
            for v in server.env
        ]
        # Every declared variable is encrypted, not just the ones flagged secret:
        # which of them are sensitive is the server's claim, and a wrong claim
        # should not put a token in a plaintext column.
        secrets = {f"env_{name}": value for name, value in values.items() if value}

        instance = BackendInstance(slug=slug, title=title, plugin_id=plugin.id,
                                   config=config, secrets=secrets)
        try:
            config.update(await plugin.on_save(instance) or {})
        except Exception:  # noqa: BLE001 - the backend is still worth creating
            log.exception("could not introspect %s while adding it", server.name)

        # Created disabled: the tools come from someone else's code, so they are
        # reviewed on the settings page before anything is exposed.
        _save_backend(hub, slug=slug, plugin_id=plugin.id, title=title, enabled=False,
                      config=config, secrets=secrets)
        return RedirectResponse(f"/backends/{slug}", status_code=303)

    # ── accounts ──────────────────────────────────────────────────────────

    async def accounts(request: Request) -> Response:
        user = require_user(request)
        if not user:
            return redirect_to_login(request)
        if not is_admin(user):
            return denied(request, "Only an administrator can manage accounts.")

        rows = hub.db.query("SELECT * FROM users ORDER BY username")
        backends = hub.backend_rows()
        listing = [{
            "id": r["id"], "username": r["username"],
            "is_admin": bool(r["is_admin"]), "can_add": bool(r["can_add_backends"]),
            "is_you": r["id"] == user["id"],
            "grants": sorted(granted_backends(r["id"])),
        } for r in rows]
        return render(request, "accounts.html", accounts=listing, backends=backends,
                      errors=request.query_params.getlist("error"))

    async def account_create(request: Request) -> Response:
        user = require_user(request)
        if not user:
            return redirect_to_login(request)
        if not is_admin(user):
            return denied(request, "Only an administrator can manage accounts.")

        form = await request.form()
        username = str(form.get("username", "")).strip().lower()
        password = str(form.get("password", ""))

        if not re.match(r"^[a-z0-9][a-z0-9._-]{1,30}$", username):
            return RedirectResponse("/accounts?error=Username+must+be+2-31+characters%2C+"
                                    "letters+digits+dot+dash+underscore.", status_code=303)
        if len(password) < 12:
            return RedirectResponse("/accounts?error=Password+must+be+at+least+12+characters.",
                                    status_code=303)
        if hub.db.one("SELECT id FROM users WHERE username = ?", (username,)):
            return RedirectResponse(f"/accounts?error=An+account+named+{username}+already+exists.",
                                    status_code=303)

        hub.db.execute(
            "INSERT INTO users (username, password_hash, is_admin, can_add_backends, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (username, hash_password(password), int(form.get("is_admin") is not None),
             int(form.get("can_add_backends") is not None), utcnow()),
        )
        log.info("account %r created by %r", username, user["username"])
        return RedirectResponse("/accounts", status_code=303)

    async def account_update(request: Request) -> Response:
        user = require_user(request)
        if not user:
            return redirect_to_login(request)
        if not is_admin(user):
            return denied(request, "Only an administrator can manage accounts.")

        target = hub.db.one("SELECT * FROM users WHERE id = ?", (request.path_params["user_id"],))
        if target is None:
            return render(request, "error.html", message="No such account.", status_code=404)

        form = await request.form()
        wants_admin = form.get("is_admin") is not None

        # Removing your own admin rights, as the only admin, would leave a hub
        # nobody can administer and no way back in.
        if target["id"] == user["id"] and not wants_admin:
            others = hub.db.one("SELECT COUNT(*) AS n FROM users WHERE is_admin = 1 AND id != ?",
                                (user["id"],))
            if not others or not others["n"]:
                return RedirectResponse(
                    "/accounts?error=You+are+the+only+administrator%3B+promote+someone+else+first.",
                    status_code=303)

        hub.db.execute("UPDATE users SET is_admin = ?, can_add_backends = ? WHERE id = ?",
                       (int(wants_admin), int(form.get("can_add_backends") is not None), target["id"]))

        wanted = {str(v) for v in form.getlist("grant")}
        hub.db.execute("DELETE FROM backend_grants WHERE user_id = ?", (target["id"],))
        for row in hub.backend_rows():
            if row["slug"] in wanted:
                hub.db.execute(
                    "INSERT INTO backend_grants (user_id, backend_id, created_at) VALUES (?, ?, ?)",
                    (target["id"], row["id"], utcnow()))
        log.info("account %r updated by %r; grants now %s",
                 target["username"], user["username"], sorted(wanted))
        return RedirectResponse("/accounts", status_code=303)

    async def account_delete(request: Request) -> Response:
        user = require_user(request)
        if not user:
            return redirect_to_login(request)
        if not is_admin(user):
            return denied(request, "Only an administrator can manage accounts.")

        target_id = int(request.path_params["user_id"])
        if target_id == user["id"]:
            return RedirectResponse("/accounts?error=You+cannot+delete+the+account+you+are+using.",
                                    status_code=303)

        # Their tokens and browser sessions go with them, or a deleted account
        # keeps working until whatever it holds happens to expire.
        hub.db.execute("DELETE FROM tokens WHERE user_id = ?", (target_id,))
        hub.db.execute("DELETE FROM web_sessions WHERE user_id = ?", (target_id,))
        hub.db.execute("DELETE FROM auth_codes WHERE user_id = ?", (target_id,))
        hub.db.execute("DELETE FROM backend_grants WHERE user_id = ?", (target_id,))
        hub.db.execute("DELETE FROM users WHERE id = ?", (target_id,))
        return RedirectResponse("/accounts", status_code=303)

    return [
        Route("/", dashboard),
        Route("/accounts", accounts),
        Route("/accounts/new", account_create, methods=["POST"]),
        Route("/accounts/{user_id:int}", account_update, methods=["POST"]),
        Route("/accounts/{user_id:int}/delete", account_delete, methods=["POST"]),
        Route("/registry", registry_search),
        Route("/registry/add", registry_add, methods=["GET", "POST"]),
        Route("/login", login, methods=["GET", "POST"]),
        Route("/logout", logout, methods=["GET", "POST"]),
        Route("/account", account, methods=["GET", "POST"]),
        Route("/backends/new", backend_form, methods=["GET", "POST"]),
        Route("/backends/{slug}", backend_form, methods=["GET", "POST"]),
        Route("/backends/{slug}/test", backend_test, methods=["POST"]),
        Route("/backends/{slug}/refresh", backend_refresh, methods=["POST"]),
        Route("/backends/{slug}/versions", backend_versions),
        Route("/backends/{slug}/pin", backend_pin, methods=["POST"]),
        Route("/backends/{slug}/delete", backend_delete, methods=["POST"]),
    ]


async def not_found(request: Request) -> Response:
    return HTMLResponse("<h1>404</h1><p>Nothing here.</p>", status_code=404)
