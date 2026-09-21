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
from urllib.parse import quote, urlparse
from typing import Any

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route
from starlette.templating import Jinja2Templates

from ..crypto import hash_password, verify_password
from ..db import utcnow
from .. import registry as mcp_registry
from .. import appaccess
from .. import roles
from .. import storage
from ..plugins.builtin.hub import SLUG as HUB_SLUG
from ..plugins.base import (
    CLEAR_PREFIX,
    BackendInstance,
    ConfigField,
    FieldError,
    choice_pairs,
    show_if_values,
    as_field_errors,
)
from .session import current_user, end_session, start_session
from .uiproxy import check as check_ui
from .uiproxy import forward as proxy_ui
from .uiproxy import is_proxyable

log = logging.getLogger(__name__)

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,38}[a-z0-9]$")

NEW_BACKEND = "new"
"""Stands in for the slug of a backend that does not exist yet.

`/backends/new` is the create form, so `/backends/new/test` is "test what I am
typing" — which needs no special route, because the slug pattern matches it and
no real backend can hold the name.
"""

# Reserved because a backend mounted at one of these would shadow the hub's
# own routes and, in the case of the OAuth endpoints, break authentication
# for every other backend at the same time.
#
# `new` shadows nothing so dramatically, but `/backends/new` is registered
# ahead of `/backends/{slug}` and both lead to the same handler, so a backend
# named that could be created and then never opened again: its settings page
# would forever render the blank create form instead.
RESERVED_SLUGS = {"login", "logout", "account", "accounts", "backends", "healthz", "mcp", "ui",
                  "authorize", "token", "register", "registry", "revoke", NEW_BACKEND,
                  # The hub's own backend. Reserved so nothing else can take the
                  # name, and kept by the one thing that is allowed to have it.
                  HUB_SLUG}


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


def _split_env_blob(blob: str, declared: list[str]) -> tuple[dict[str, str], str]:
    """Move declared variables out of the freeform block into their own keys.

    Otherwise a relinked backend shows seven empty typed fields while its
    actual values sit in a blob the form does not display — working, but
    reading as though the configuration had been lost.
    """
    from ..plugins.builtin.mcpproxy import _parse_env

    values = _parse_env(blob)
    wanted = {name: values.pop(name) for name in declared if name in values}
    leftover = "\n".join(f"{k}={v}" for k, v in values.items())
    return wanted, leftover


LINK_ATTEMPTED_KEY = "registry_link_attempted_at"


async def relink_and_save(hub: Any, row: Any) -> bool:
    """Relink one backend and persist it. Returns True if anything changed.

    Records the attempt either way, so a backend whose command is not in the
    registry is not looked up again on every page load.
    """
    recovered = await relink_registry(hub, row)
    instance = hub.instance_from_row(row)
    config = {**json.loads(row["config_json"]), LINK_ATTEMPTED_KEY: utcnow()}
    secrets = dict(instance.secrets)

    if recovered:
        config.update(recovered)
        declared = [v["name"] for v in recovered.get("registry_env") or []]
        moved, leftover = _split_env_blob(str(secrets.get("env") or ""), declared)
        for name, value in moved.items():
            secrets.setdefault(f"env_{name}", value)
        if moved:
            secrets["env"] = leftover
            if not leftover:
                secrets.pop("env", None)
            log.info("backend %s: moved %d variable(s) into their own fields",
                     row["slug"], len(moved))

    _save_backend(hub, slug=row["slug"], plugin_id=row["plugin_id"], title=row["title"],
                  enabled=bool(row["enabled"]), config=config, secrets=secrets, row=row)
    return bool(recovered)


def needs_relink(row: Any) -> bool:
    try:
        config = json.loads(row["config_json"])
    except (json.JSONDecodeError, TypeError):
        return False
    if config.get("registry_name") and config.get("registry_package"):
        return False
    if config.get(LINK_ATTEMPTED_KEY):
        return False
    return bool(str(config.get("command") or "").strip())


async def relink_registry(hub: Any, row: Any) -> dict[str, Any] | None:
    """Re-attach a backend to the registry entry its command launches.

    Two backends need this. One added from the registry before saving the form
    preserved config, whose metadata a save discarded; and one added by hand,
    which never had any. Both end up showing a freeform environment box with an
    example about somebody else's API key, instead of the variables their
    server actually declares.

    Only ever adds. A command that matches nothing, or matches more than one
    published server, is left exactly as it is.
    """
    try:
        config = json.loads(row["config_json"])
    except (json.JSONDecodeError, TypeError):
        return None
    if config.get("registry_name") and config.get("registry_package"):
        return None
    command = str(config.get("command") or "").strip()
    if not command:
        return None

    _, identifier = mcp_registry.package_from_command(command)
    try:
        server = await mcp_registry.find_by_package(identifier)
    except mcp_registry.RegistryError as exc:
        log.info("could not look up %r while relinking %s: %s", identifier, row["slug"], exc)
        return None
    if server is None or not server.package:
        return None

    log.info("backend %s relinked to %s", row["slug"], server.name)
    return {
        "registry_name": server.name,
        "registry_package": {
            "registryType": server.package.registry_type,
            "identifier": server.package.identifier,
            "runtime": server.package.runtime,
            "args": list(server.package.args),
        },
        "registry_env": [
            {"name": v.name, "description": v.description,
             "isRequired": v.required, "isSecret": v.secret}
            for v in server.env
        ],
        **({"registry_icons": list(server.icons)} if server.icons else {}),
    }


def _resource_slug(resource: str | None) -> str | None:
    """The backend a token is being requested for, from its RFC 8707 resource."""
    if not resource:
        return None
    path = urlparse(str(resource)).path.rstrip("/")
    marker = "/mcp/"
    return path[path.rindex(marker) + len(marker):] if marker in path else None


def _revoke_backend_credentials(hub: Any, slug: str) -> int:
    """Drop every credential minted for this backend's endpoint.

    Tokens carry the endpoint as an RFC 8707 resource string and nothing links
    them back to the row, so deleting a backend left them behind. Recreate the
    same slug later and those strings match again — the per-request grant check
    still refuses, since the grants went with the row, but a credential
    outliving the thing it was issued for is not worth keeping around.

    Matched through `_resource_slug`, the same rule authorization uses, rather
    than by comparing whole URLs: a hub whose public URL has changed since a
    token was issued must still recognise its own.
    """
    removed = 0
    for row in hub.db.query("SELECT token_hash, resource FROM tokens"):
        if _resource_slug(row["resource"]) == slug:
            hub.db.execute("DELETE FROM tokens WHERE token_hash = ?", (row["token_hash"],))
            removed += 1
    for row in hub.db.query("SELECT code, resource FROM auth_codes"):
        if _resource_slug(row["resource"]) == slug:
            hub.db.execute("DELETE FROM auth_codes WHERE code = ?", (row["code"],))
            removed += 1
    return removed


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
        # A renamed backend takes its files with it. Leaving them behind would
        # look like the rename had wiped them, and the old directory would sit
        # there unattached to anything.
        storage.rename(hub.settings.data_dir, row["slug"], slug)
        hub.db.execute(
            "UPDATE backends SET slug = ?, title = ?, enabled = ?, config_json = ?, "
            "secrets_blob = ?, updated_at = ? WHERE id = ?",
            (slug, title, int(enabled), json.dumps(config), blob, utcnow(), row["id"]),
        )
    # Created at save rather than only at mount, so the path is there to be
    # bind-mounted before the thing that needs it is started.
    storage.ensure(hub.settings.data_dir, slug)


def stored_value(instance: BackendInstance | None, key: str) -> Any:
    """What is saved for `key`, secret or plain, or None if nothing is."""
    if instance is None:
        return None
    if key in instance.secrets:
        return instance.secrets[key]
    return instance.config.get(key)


async def form_values(plugin: Any, instance: BackendInstance | None,
                      posted: Any = None) -> list[dict[str, Any]]:
    """One render-ready entry per field of this backend's form.

    `posted` is the submission being redisplayed after a validation error. Its
    values win over the stored ones, so a rejected save does not also throw away
    everything typed alongside the mistake.

    Three things the template needs, kept apart because they differ per field:
    `value` is what goes in the control, `stored` says something is saved, and
    `withheld` says something is saved that will not be shown — the case that
    needs marking, or the box reads as empty when it is not.
    """
    values = []
    for f in plugin.fields_for(instance):
        options: list[Any] = []
        selected: list[str] = []
        saved = stored_value(instance, f.key)
        # A checkbox saved as False is still a saved answer; an empty string
        # anywhere else is the absence of one.
        stored = saved is not None if f.type == "bool" else saved not in (None, "")
        withheld = stored and not f.shows_value

        value: Any
        if withheld:
            # A secret is never echoed back, not even one the user just typed
            # into a submission that failed validation.
            value = ""
        elif posted is not None and f.type != "multiselect":
            value = posted.get(f.key) is not None if f.type == "bool" else posted.get(f.key, "")
        elif stored:
            value = saved
        else:
            value = f.default

        if f.asks_the_plugin_for_choices and instance is not None:
            # Fetched live: the choices belong to the upstream, not to us. A
            # failure here leaves the list empty rather than breaking the page,
            # and whatever was saved is still shown.
            options = list(await plugin.options(instance, f.key))

        if f.type == "multiselect":
            raw = (posted.getlist(f.key) if posted is not None else saved) or []
            selected = list(raw) if isinstance(raw, list) else [v for v in str(raw).split(",") if v]
            value = ""

        values.append({
            "field": f, "value": "" if value is None else value,
            "stored": stored, "withheld": withheld, "wanted": show_if_values(f),
            "options": options, "selected": selected, "choices": choice_pairs(f),
        })
    return values


def orphaned_secrets(plugin: Any, instance: BackendInstance | None) -> list[str]:
    """Stored secrets that no field of this backend's form accounts for.

    An upstream that drops a variable from its declaration leaves its value
    behind, and for the proxy plugin that orphan is not inert: `_collect_env`
    hands every stored `env_*` key to the launched server whether or not
    anything still declares it. So the value goes on being passed, through a
    box that is no longer drawn.

    They are shown rather than pruned. Deleting one on the quiet would change
    what the server receives exactly as silently as keeping it does, and this
    is the half of the bargain the form can actually offer: it cannot edit a
    value it has no field for, but it can say the value is there and let it go.
    """
    if instance is None:
        return []
    declared = {f.key for f in plugin.fields_for(instance)}
    return sorted(key for key in instance.secrets if key not in declared)


def run_plugin_validation(plugin: Any, instance: BackendInstance) -> list[FieldError]:
    """Run the plugin's own validation, failing closed.

    A validator that raises cannot be waved through the way `on_save` is: the
    point of the hook is to stop a configuration, so a broken one has to stop
    it too. Swallowing the exception would let exactly the config the plugin
    meant to refuse be the one that gets saved.
    """
    try:
        return as_field_errors(plugin.validate(instance))
    except Exception as exc:  # noqa: BLE001 - reported on the form, not raised
        log.exception("validate hook failed for backend %s", instance.slug)
        return [FieldError(
            f"{plugin.id} could not check this configuration: {type(exc).__name__}: {exc}"
        )]


def place_errors(problems: list[FieldError], keys: set[str]) -> tuple[list[str], dict[str, list[str]]]:
    """Split problems into the banner and the per-field notes.

    A problem naming a field the form is not showing would otherwise be
    rendered nowhere at all, so anything unplaceable falls back to the banner
    rather than disappearing — silently losing the reason a save was refused
    is the one outcome worse than an ugly one.
    """
    banner: list[str] = []
    beside: dict[str, list[str]] = {}
    for problem in problems:
        if problem.key and problem.key in keys:
            beside.setdefault(problem.key, []).append(problem.message)
        else:
            banner.append(problem.message)
    return banner, beside


def split_fields(plugin: Any, form: Any, existing: BackendInstance | None) -> tuple[dict, dict, list[str]]:
    config: dict[str, Any] = {}
    secret: dict[str, Any] = dict(existing.secrets) if existing else {}
    errors: list[str] = []

    fields = list(plugin.fields_for(existing))
    for f in fields:
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
                # A typed value settles it, even against a ticked clear box:
                # of two contradictory instructions it is the specific one.
                secret[f.key] = text
            elif f.shows_value or form.get(f"{CLEAR_PREFIX}{f.key}") is not None:
                # Either the box was rendered with the saved value in it, so an
                # empty box was emptied on purpose, or the clear box says so
                # outright. Only a field that renders blank no matter what can
                # read blank as "unchanged".
                secret.pop(f.key, None)
                if f.required:
                    errors.append(f"{f.label} is required.")
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

    # A stored secret with no field left to render it cannot be edited here, so
    # removal is the only thing the form can offer — and only when asked.
    declared = {f.key for f in fields}
    for key in list(secret):
        if key not in declared and form.get(f"{CLEAR_PREFIX}{key}") is not None:
            secret.pop(key)
    return config, secret, errors


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

    def grant_levels(user_id: int) -> dict[str, str]:
        """Every backend this account was granted, and how far each one goes."""
        return {r["slug"]: roles.normalise(r["role"]) for r in hub.db.query(
            "SELECT b.slug, g.role FROM backend_grants g JOIN backends b ON b.id = g.backend_id "
            "WHERE g.user_id = ?", (user_id,))}

    def granted_backends(user_id: int) -> set[str]:
        return set(grant_levels(user_id))

    def app_access(row: Any) -> dict[str, Any]:
        """What the settings form needs for the "backends this app may use" picker.

        A backend can be granted others, but only once it exists — it needs an
        identity of its own, and that is keyed by its URL name.
        """
        if row is None:
            return {"app_grants": {}, "app_targets": []}
        return {
            "app_grants": hub.apps.grants(row["slug"]),
            # Not itself: an app reaching itself through the hub would be a
            # loop with nothing in it.
            "app_targets": [b for b in hub.backend_rows() if b["slug"] != row["slug"]],
        }

    def level_for(user: dict[str, Any] | None, slug: str) -> str:
        """An account's level on one backend. Admins hold every backend outright."""
        if is_admin(user):
            return roles.ADMIN
        return grant_levels(user["id"]).get(slug, roles.VIEWER) if user else roles.VIEWER

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

        # Heal here too, not only on the settings page. This is the page where
        # a missing version selector would be noticed, and it was the page that
        # could never produce one. Attempted once per backend, so a command
        # that is not in the registry is not looked up on every load.
        if may_manage_backends(user):
            for candidate in hub.backend_rows():
                if needs_relink(candidate):
                    try:
                        await relink_and_save(hub, candidate)
                    except Exception:  # noqa: BLE001 - the page still renders
                        log.exception("relink failed for %s", candidate["slug"])

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
                "level": level_for(user, row["slug"]),
                "version": _config_value(row, "upstream_version"),
                "latest": _config_value(row, "latest_version"),
                "ui_url": _config_value(row, "ui_url"),
                "ui_proxied": bool(_config_value(row, "ui_url")
                                   and json.loads(row["config_json"]).get("ui_proxy", True)),
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
                      plugins=[p for p in hub.registry.all() if p.id != HUB_SLUG]
                      if may_manage_backends(user) else [],
                      can_manage=may_manage_backends(user), is_admin=is_admin(user),
                      level_help=roles.DESCRIPTIONS,
                      errors=request.query_params.getlist("error"))

    # ── backend create / edit ─────────────────────────────────────────────

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

        # A backend with no registry metadata gets one look-up to find it. The
        # alternative is a form that asks for a freeform blob forever, for a
        # server that publishes exactly what it needs.
        if row is not None and request.method == "GET":
            if needs_relink(row) or not json.loads(row["config_json"]).get("registry_name"):
                try:
                    await relink_and_save(hub, row)
                except Exception:  # noqa: BLE001 - the form still renders
                    log.exception("relink failed for %s", row["slug"])
                row = hub.backend_row(row["slug"])
                instance = hub.instance_from_row(row)

        if request.method == "GET":
            # The page tells the administrator to bind-mount this path, so it
            # should be there when they go and do it — including for a backend
            # that predates storage and has not been saved since.
            if row is not None:
                storage.ensure(hub.settings.data_dir, row["slug"])
            return render(request, "backend_form.html", plugin=plugin, row=row,
                          fields=await form_values(plugin, instance), errors=[], field_errors={},
                          original_slug=slug or NEW_BACKEND,
                          orphans=orphaned_secrets(plugin, instance),
                          storage_path=instance.storage if instance else None,
                          storage_var=storage.ENV_VAR,
                          **app_access(row), levels=roles.LEVELS,
                          level_help=roles.DESCRIPTIONS, default_level=roles.DEFAULT,
                          pinnable=bool(row and instance
                                        and instance.config.get("registry_name")
                                        and instance.config.get("registry_package")),
                          slug=slug or "", title=row["title"] if row else "",
                          # A plugin whose tool surface comes from elsewhere starts
                          # disabled, so its tools are reviewed before they attach.
                          enabled=bool(row["enabled"]) if row
                          else not plugin.review_before_enable)

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
        elif new_slug in RESERVED_SLUGS and new_slug != slug:
            # Reserved against *taking* the name, not against a backend that
            # already has it keeping it — which is how the hub's own backend
            # can be edited at all.
            errors.append(f"{new_slug!r} is reserved — pick another URL name.")
        elif new_slug != slug and hub.backend_row(new_slug) is not None:
            errors.append(f"A backend with the URL name {new_slug!r} already exists.")
        if slug == HUB_SLUG and new_slug != slug:
            errors.append("This hub's own backend keeps its URL name; clients and its "
                          "documentation both refer to it by that name.")
        if not title:
            errors.append("Display name is required.")

        proposed = BackendInstance(slug=new_slug, title=title, plugin_id=plugin.id,
                                   config=config, secrets=secret)
        # Only once the declared constraints hold. Asking the plugin whether a
        # host is reachable while the host box is still empty produces a second
        # complaint about the same blank field, and the two disagree about what
        # is wrong with it.
        problems = [] if errors else run_plugin_validation(plugin, proposed)

        if errors or problems:
            banner, beside = place_errors(problems, {f.key for f in plugin.fields_for(instance)})
            # Redisplayed from the submission, not from what is stored: a
            # rejected slug should not also silently revert every other box on
            # the page to its saved value.
            return render(request, "backend_form.html", plugin=plugin, row=row,
                          fields=await form_values(plugin, instance, posted=form),
                          errors=errors + banner, field_errors=beside,
                          original_slug=slug or NEW_BACKEND,
                          orphans=orphaned_secrets(plugin, instance),
                          storage_path=instance.storage if instance else None,
                          storage_var=storage.ENV_VAR,
                          **app_access(row), levels=roles.LEVELS,
                          level_help=roles.DESCRIPTIONS, default_level=roles.DEFAULT,
                          pinnable=bool(row and instance
                                        and instance.config.get("registry_name")
                                        and instance.config.get("registry_package")),
                          slug=new_slug, title=title, enabled=enabled, status_code=400)

        # Give the plugin a chance to cache what it discovered (an upstream tool
        # catalogue, say) so that `build` never needs the network. A failure here
        # must not lose the user's edits, so it is folded in and ignored.
        try:
            discovered = await plugin.on_save(proposed)
            config.update(discovered or {})
        except Exception:  # noqa: BLE001 - saving is the priority
            log.exception("on_save hook failed for backend %s", new_slug)

        _save_backend(hub, slug=new_slug, plugin_id=plugin.id, title=title, enabled=enabled,
                      config=config, secrets=secret, row=row)
        if row is not None and slug != new_slug:
            hub.apps.rename(slug, new_slug)
            await hub.mounts.unmount(slug)

        # After the save, so a grant can name a backend that is only now called
        # what it is called. Replaces rather than merges: the picker shows every
        # candidate, so what came back is the whole answer.
        if row is not None:
            wanted = {str(v) for v in form.getlist("app_grant")}
            hub.apps.set_grants(new_slug, {
                target: str(form.get(f"app_level-{target}", ""))
                for target in wanted
            })

        error = await hub.remount(new_slug)
        if error:
            return render(request, "error.html",
                          message=f"Saved, but the backend could not be started: {error}", status_code=500)
        return RedirectResponse("/", status_code=303)

    async def backend_test(request: Request) -> Response:
        """Live connectivity check, for two callers that mean different things.

        The dashboard asks about a backend *as saved* and posts nothing. The
        settings page asks about what is *typed*, and posts the form — so a
        credential can be proved before it is committed rather than after,
        which is the only order that helps at all when the backend is new and
        has nothing saved to fall back on.
        """
        user = require_user(request)
        if not user:
            return JSONResponse({"ok": False, "detail": "Not signed in."}, status_code=401)

        slug = request.path_params["slug"]
        form = await request.form()
        row = hub.backend_row(slug)

        if form.get("plugin_id") is None:
            if not may_use(user, slug):
                return JSONResponse({"ok": False, "detail": "No access to this backend."}, status_code=403)
            if row is None:
                return JSONResponse({"ok": False, "detail": "No such backend."}, status_code=404)
            plugin = hub.registry.get(row["plugin_id"])
            if plugin is None:
                return JSONResponse({"ok": False, "detail": f"Plugin {row['plugin_id']!r} is not installed."})
            instance = hub.instance_from_row(row)
            result = await plugin.check(instance)
            return JSONResponse({"ok": result.ok, "detail": result.detail})

        # Testing what was typed means running it, and for the proxy plugin a
        # posted command is an arbitrary program to launch. That is the right
        # to *configure* a backend, not the right to use one — a distinction
        # the saved-backend branch above does not have to make, because there
        # the values were configured by someone who already had it.
        if not may_manage_backends(user):
            return JSONResponse({"ok": False, "detail": "This account cannot configure backends."},
                                status_code=403)
        plugin_id = str(form.get("plugin_id", ""))
        plugin = hub.registry.get(plugin_id)
        if plugin is None:
            return JSONResponse({"ok": False, "detail": f"Plugin {plugin_id!r} is not installed."},
                                status_code=404)

        existing = hub.instance_from_row(row) if row is not None else None
        posted, secrets, errors = split_fields(plugin, form, existing)
        if errors:
            return JSONResponse({"ok": False, "detail": " ".join(errors)})
        # Overlaid the same way a save does, so the test runs against what a
        # save would actually store rather than the form alone.
        instance = BackendInstance(
            slug=slug, title=str(form.get("title", "")).strip() or slug, plugin_id=plugin.id,
            config={**(existing.config if existing else {}), **posted}, secrets=secrets,
        )

        # Ask the plugin before going near the network. "Connection refused" is
        # a poor way to learn that the URL had no scheme, and a launch with no
        # command has nothing to refuse the connection in the first place.
        problems = run_plugin_validation(plugin, instance)
        if problems:
            labels = {f.key: f.label for f in plugin.fields_for(existing)}
            return JSONResponse({"ok": False, "detail": "; ".join(
                f"{labels[p.key]}: {p.message}" if p.key in labels else p.message
                for p in problems)})

        result = await plugin.check(instance)
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
        was = plugin.tool_names(before)
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
        now = plugin.tool_names(after)
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
        row = hub.backend_row(slug)
        if row is None:
            return RedirectResponse("/", status_code=303)
        if slug == HUB_SLUG:
            return denied(request, "This is the hub's own backend, so it is not yours to "
                                   "delete — it would be back on the next restart. Disable "
                                   "it instead if you do not want it exposed.")

        # The endpoint comes down first, so nothing new arrives while the
        # plugin is releasing whatever this backend holds.
        await hub.mounts.unmount(slug)

        note = ""
        plugin = hub.registry.get(row["plugin_id"])
        if plugin is None:
            note = (f"{slug!r} was removed, but its plugin {row['plugin_id']!r} is not "
                    "installed, so anything it had set up elsewhere was left alone.")
        else:
            try:
                await plugin.on_delete(hub.instance_from_row(row))
            except Exception as exc:  # noqa: BLE001 - the removal still goes ahead
                log.exception("on_delete hook failed for backend %s", slug)
                note = (f"{slug!r} was removed, but {plugin.id} could not finish cleaning "
                        f"up after it: {type(exc).__name__}: {exc}")

        released = _revoke_backend_credentials(hub, slug)
        # The app's own identity goes with it. An account for a backend that is
        # no longer there can reach nothing, and is only a row to wonder about.
        hub.apps.forget(slug)
        hub.db.execute("DELETE FROM backends WHERE slug = ?", (slug,))
        log.info("deleted backend %s, releasing %d credential(s)", slug, released)
        return RedirectResponse(f"/?error={quote(note)}" if note else "/", status_code=303)

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

    # ── a backend's own web interface ─────────────────────────────────────

    async def backend_ui(request: Request) -> Response:
        """Serve a backend's interface behind this hub's sign-in.

        The value is the access control: an interface with no login of its own
        gets one, reachable at a path on a hostname that already exists rather
        than a new one published for it.

        Authorised by the browser session, not a bearer token, and by the same
        grant that governs the MCP endpoint — an account that cannot use a
        backend cannot see its interface either.
        """
        slug = request.path_params["slug"]
        user = require_user(request)
        if not user:
            return redirect_to_login(request)
        if not may_use(user, slug):
            return denied(request, "This account has not been granted access to that backend.")

        row = hub.backend_row(slug)
        if row is None:
            return render(request, "error.html", message=f"No backend named {slug!r}.",
                          status_code=404)
        instance = hub.instance_from_row(row)
        target = str(instance.get("ui_url", "") or "").strip()
        if not target or not is_proxyable(target):
            return render(request, "error.html",
                          message=f"{row['title']} has no web interface configured.",
                          status_code=404)
        if not instance.config.get("ui_proxy", True):
            return render(request, "error.html",
                          message=f"{row['title']}'s interface is set to be opened directly, "
                                  "not served through the hub.", status_code=404)

        trusted = bool(instance.config.get("ui_trusted"))
        # Only a trusted app is told who is signed in. Handing an identity to a
        # sandboxed page would be pointless anyway — it cannot act on it — and
        # would leak the account name to something not vouched for.
        identity = {
            "x-mcphub-user": str(user["username"]),
            "x-mcphub-admin": "1" if is_admin(user) else "0",
            # The hub can enforce a level over MCP, where tools say what they
            # do. It cannot over HTTP, where a POST is just a POST — so an app
            # is told the level and decides for itself what it means.
            "x-mcphub-role": level_for(user, slug),
            # And its own credentials for whatever it was granted, which are
            # the app's rather than this person's.
            **hub.apps.header(slug),
        } if trusted else None
        return await proxy_ui(request, target, f"/ui/{slug}/",
                              trusted=trusted, identity=identity)

    async def backend_ui_check(request: Request) -> Response:
        """Report what would stop a backend's interface working through the hub."""
        user = require_user(request)
        slug = request.path_params["slug"]
        if not user:
            return JSONResponse({"ok": False, "detail": "Not signed in."}, status_code=401)
        if not may_manage_backends(user):
            return JSONResponse({"ok": False, "detail": "This account cannot configure backends."},
                                status_code=403)
        row = hub.backend_row(slug)
        if row is None:
            return JSONResponse({"ok": False, "detail": "No such backend."}, status_code=404)

        target = str(hub.instance_from_row(row).get("ui_url", "") or "").strip()
        if not target or not is_proxyable(target):
            return JSONResponse({"ok": False, "detail": "No web interface is configured."})
        return JSONResponse(await check_ui(target, f"/ui/{slug}/"))

    async def backend_ui_root(request: Request) -> Response:
        # The bare mount has no trailing slash, so every relative link on the
        # page would resolve one level too high. Redirecting once fixes the lot.
        return RedirectResponse(f"/ui/{request.path_params['slug']}/", status_code=307)

    # ── accounts ──────────────────────────────────────────────────────────

    async def accounts(request: Request) -> Response:
        user = require_user(request)
        if not user:
            return redirect_to_login(request)
        if not is_admin(user):
            return denied(request, "Only an administrator can manage accounts.")

        rows = hub.db.query("SELECT * FROM users ORDER BY username")
        backends = hub.backend_rows()
        # An app has an account so that the grant machinery applies to it, but
        # it is not a person: editing it here would offer to make a backend an
        # administrator. It is listed below instead, where it can be seen and
        # revoked, and it is changed on the app's own settings page.
        listing = [{
            "id": r["id"], "username": r["username"],
            "is_admin": bool(r["is_admin"]), "can_add": bool(r["can_add_backends"]),
            "is_you": r["id"] == user["id"],
            "levels": grant_levels(r["id"]),
        } for r in rows if not appaccess.is_app(r["username"])]
        apps = [{
            "slug": r["username"][len(appaccess.PREFIX):],
            "levels": grant_levels(r["id"]),
        } for r in rows if appaccess.is_app(r["username"])]
        return render(request, "accounts.html", accounts=listing, backends=backends,
                      apps=apps, levels=roles.LEVELS, level_help=roles.DESCRIPTIONS,
                      default_level=roles.DEFAULT,
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
        granted: dict[str, str] = {}
        for row in hub.backend_rows():
            if row["slug"] not in wanted:
                continue
            # The select is submitted whether or not the box is ticked, so it is
            # read here rather than trusted to be absent for an ungranted one.
            level = roles.normalise(str(form.get(f"level-{row['slug']}", "")))
            granted[row["slug"]] = level
            hub.db.execute(
                "INSERT INTO backend_grants (user_id, backend_id, role, created_at) "
                "VALUES (?, ?, ?, ?)",
                (target["id"], row["id"], level, utcnow()))
        log.info("account %r updated by %r; grants now %s",
                 target["username"], user["username"], sorted(granted.items()))
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
        Route("/backends/{slug}/ui-check", backend_ui_check, methods=["POST"]),
        Route("/ui/{slug}", backend_ui_root),
        Route("/ui/{slug}/", backend_ui, methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]),
        Route("/ui/{slug}/{path:path}", backend_ui,
              methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]),
    ]


async def not_found(request: Request) -> Response:
    return HTMLResponse("<h1>404</h1><p>Nothing here.</p>", status_code=404)
