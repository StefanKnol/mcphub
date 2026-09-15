"""MikroTik tools.

Two rules shape this module:

1. **Writes address rules by `.id`, never by position.** Every read returns the
   device's real `.id`; every write takes one back. If a rule is gone, the
   write fails cleanly instead of silently hitting whatever slid into its
   place. Positional indices are reported for humans and rejected for writes.
2. **Writes read back what they wrote**, so a caller can verify the change
   landed rather than trusting an "ok".
"""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from .client import RouterError, RouterOS

READ = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=False)
WRITE = ToolAnnotations(destructive_hint=False, idempotent_hint=True, open_world_hint=False)
DESTRUCTIVE = ToolAnnotations(destructive_hint=True, idempotent_hint=True, open_world_hint=False)

FIREWALL = ("ip", "firewall", "filter")
NAT = ("ip", "firewall", "nat")

# Fields worth showing without being asked. The device returns ~30 per rule,
# most of them counters and defaults; dumping all of them for every rule costs
# a great deal of context to say very little.
RULE_SUMMARY = (
    ".id", "chain", "action", "comment", "disabled", "dynamic", "protocol",
    "src-address", "dst-address", "src-port", "dst-port", "in-interface",
    "out-interface", "in-interface-list", "out-interface-list",
    "connection-state", "connection-nat-state", "src-address-list",
    "dst-address-list", "to-addresses", "to-ports", "log", "log-prefix",
)


def _summarise(rows: list[dict[str, Any]], keys: tuple[str, ...], verbose: bool) -> list[dict[str, Any]]:
    out = []
    for position, row in enumerate(rows):
        item = dict(row) if verbose else {k: v for k, v in row.items() if k in keys}
        # `position` is display-only. It is what the CLI would print, and it is
        # exactly the thing that must never be used as a write handle.
        item["position"] = position
        item["id"] = item.pop(".id", row.get(".id"))
        out.append(item)
    return out


def _dump(payload: Any) -> str:
    return json.dumps(payload, indent=2, sort_keys=False, default=str)


def _require_real_id(rule_id: str, kind: str = "rule") -> str:
    """Reject positional indices at the door.

    This is the single guard that the server this replaces was missing. There,
    `list` printed positions, the docs told you to pass one, and the write path
    looked it up as an `.id` — which cannot match — so every write reported
    "not found". Worse, had it matched, positions shift when rules are added or
    a dynamic rule appears, so the write would have hit the wrong rule.
    """
    value = rule_id.strip()
    if not value.startswith("*"):
        raise ToolError(
            f"{rule_id!r} is not a RouterOS {kind} id. Ids look like '*7' and come "
            f"from the 'id' field of a list result — the 'position' field is a "
            f"display index that shifts whenever rules change, so it is not accepted "
            f"for writes. List the {kind}s and pass the 'id' of the one you mean."
        )
    return value


class _Surfaced:
    """Re-raises device failures as `ToolError` so their text reaches the model.

    Anything that is not a `ToolError` is treated by the SDK as a crash and the
    model is told only "Error executing tool X" — which for "the router refused
    this address" or "the credentials are wrong" is precisely the wrong half of
    the message to keep.
    """

    def __init__(self, router: RouterOS) -> None:
        self._router = router

    def __getattr__(self, name: str):
        attr = getattr(self._router, name)

        async def wrapper(*args: Any, **kwargs: Any):
            try:
                return await attr(*args, **kwargs)
            except RouterError as exc:
                raise ToolError(str(exc)) from exc

        return wrapper


def register(mcp: MCPServer, device: RouterOS, *, title: str) -> None:
    """Attach every tool for one device to its own MCPServer instance."""
    router = _Surfaced(device)

    async def _list(*path: str) -> list[dict[str, Any]]:
        return await router.list(*path)

    # ── system ────────────────────────────────────────────────────────────

    @mcp.tool(name="system_info", annotations=ToolAnnotations(
        title="System Info", read_only_hint=True, idempotent_hint=True, open_world_hint=False))
    async def system_info(ctx: Context) -> str:
        """Identity, RouterOS version, model, uptime, CPU and memory for the device."""
        resource = await _list("system", "resource")
        identity = await _list("system", "identity")
        routerboard = await _list("system", "routerboard")
        return _dump({
            "device": title,
            "identity": identity[0].get("name") if identity else None,
            "resource": resource[0] if resource else {},
            "routerboard": routerboard[0] if routerboard else {},
        })

    @mcp.tool(name="ros_list", annotations=ToolAnnotations(
        title="Read Any RouterOS Path", read_only_hint=True, idempotent_hint=True, open_world_hint=False))
    async def ros_list(
        ctx: Context,
        path: Annotated[str, Field(description="RouterOS menu path, slash-separated and without the leading slash, e.g. 'ip/dhcp-server/lease' or 'interface/wireguard/peers'.")],
    ) -> str:
        """Read any RouterOS configuration path.

        The escape hatch for everything without a dedicated tool. Read-only:
        it lists items at a path and cannot modify the device.
        """
        parts = tuple(p for p in path.strip("/").replace(".", "/").split("/") if p)
        if not parts:
            raise ToolError("path must name a RouterOS menu, e.g. 'ip/address'")
        rows = await _list(*parts)
        return _dump({"path": "/".join(parts), "count": len(rows), "items": rows})

    # ── interfaces and addressing ─────────────────────────────────────────

    @mcp.tool(name="list_interfaces", annotations=ToolAnnotations(
        title="List Interfaces", read_only_hint=True, idempotent_hint=True, open_world_hint=False))
    async def list_interfaces(
        ctx: Context,
        type_filter: Annotated[str | None, Field(description="Restrict to a RouterOS interface type, e.g. 'ether', 'bridge', 'vlan', 'wg'.")] = None,
        running_only: bool = False,
    ) -> str:
        """List every interface with its type, MAC, MTU and running state."""
        rows = await _list("interface")
        if type_filter:
            rows = [r for r in rows if r.get("type", "").startswith(type_filter)]
        if running_only:
            rows = [r for r in rows if r.get("running") in (True, "true", "yes")]
        return _dump(_summarise(rows, (".id", "name", "type", "mtu", "mac-address", "running", "disabled", "comment"), False))

    @mcp.tool(name="set_interface_enabled", annotations=ToolAnnotations(
        title="Enable/Disable Interface", destructive_hint=True, idempotent_hint=True, open_world_hint=False))
    async def set_interface_enabled(
        ctx: Context,
        interface_id: Annotated[str, Field(description="The interface 'id' from list_interfaces, e.g. '*3'.")],
        enabled: bool,
    ) -> str:
        """Bring an interface up or down.

        Disabling the interface you are reaching the router through will cut
        this connection, and nothing here can undo that remotely.
        """
        item_id = _require_real_id(interface_id, "interface")
        await router.update("interface", item_id=item_id, disabled=not enabled)
        after = await router.get("interface", item_id=item_id)
        return _dump({"changed": True, "interface": after})

    @mcp.tool(name="list_ip_addresses", annotations=ToolAnnotations(
        title="List IP Addresses", read_only_hint=True, idempotent_hint=True, open_world_hint=False))
    async def list_ip_addresses(ctx: Context) -> str:
        """List configured IPv4 addresses and the interfaces they sit on."""
        rows = await _list("ip", "address")
        return _dump(_summarise(rows, (".id", "address", "network", "interface", "disabled", "dynamic", "comment"), False))

    # ── firewall filter ───────────────────────────────────────────────────

    @mcp.tool(name="list_firewall_rules", annotations=ToolAnnotations(
        title="List Firewall Rules", read_only_hint=True, idempotent_hint=True, open_world_hint=False))
    async def list_firewall_rules(
        ctx: Context,
        chain: Annotated[str | None, Field(description="Restrict to one chain: 'input', 'forward', 'output', or a custom chain name.")] = None,
        include_dynamic: Annotated[bool, Field(description="Include rules RouterOS generates itself, which cannot be edited.")] = False,
        verbose: Annotated[bool, Field(description="Return every field including packet and byte counters.")] = False,
    ) -> str:
        """List firewall filter rules in evaluation order.

        Each rule carries both an `id` and a `position`. Use `id` for any
        later change: `position` is only where the rule currently sits, and it
        shifts as soon as anything is added, removed or moved.
        """
        rows = await _list(*FIREWALL)
        summarised = _summarise(rows, RULE_SUMMARY, verbose)
        if chain:
            summarised = [r for r in summarised if r.get("chain") == chain]
        if not include_dynamic:
            summarised = [r for r in summarised if r.get("dynamic") not in (True, "true", "yes")]
        return _dump({"count": len(summarised), "rules": summarised})

    @mcp.tool(name="get_firewall_rule", annotations=ToolAnnotations(
        title="Get Firewall Rule", read_only_hint=True, idempotent_hint=True, open_world_hint=False))
    async def get_firewall_rule(
        ctx: Context,
        rule_id: Annotated[str, Field(description="The rule 'id' from list_firewall_rules, e.g. '*7'.")],
    ) -> str:
        """Every field of one firewall rule, including counters."""
        item_id = _require_real_id(rule_id)
        rule = await router.get(*FIREWALL, item_id=item_id)
        if rule is None:
            raise ToolError(f"No firewall rule with id {item_id!r} exists. It may have been removed.")
        return _dump(rule)

    @mcp.tool(name="add_firewall_rule", annotations=ToolAnnotations(
        title="Add Firewall Rule", destructive_hint=False, idempotent_hint=False, open_world_hint=False))
    async def add_firewall_rule(
        ctx: Context,
        chain: Annotated[str, Field(description="'input', 'forward', 'output', or a custom chain.")],
        action: Annotated[Literal["accept", "drop", "reject", "log", "jump", "return", "passthrough", "fasttrack-connection", "add-src-to-address-list", "add-dst-to-address-list"], Field(description="What to do with matching traffic.")],
        comment: Annotated[str | None, Field(description="Strongly recommended — an uncommented rule is very hard to audit later.")] = None,
        protocol: str | None = None,
        src_address: str | None = None,
        dst_address: str | None = None,
        src_port: str | None = None,
        dst_port: str | None = None,
        in_interface: str | None = None,
        out_interface: str | None = None,
        in_interface_list: str | None = None,
        out_interface_list: str | None = None,
        connection_state: Annotated[str | None, Field(description="Comma-separated, e.g. 'established,related'.")] = None,
        src_address_list: str | None = None,
        dst_address_list: str | None = None,
        log: bool | None = None,
        log_prefix: str | None = None,
        disabled: Annotated[bool, Field(description="Add the rule disabled, to position it before it takes effect.")] = False,
        place_before: Annotated[str | None, Field(description="Rule 'id' to insert before. Without it the rule lands at the end of the list, which for a chain ending in a drop rule usually means it never matches.")] = None,
    ) -> str:
        """Add a firewall filter rule.

        Order decides everything in a RouterOS firewall: the first matching
        rule wins. A new rule appended after a final `drop` is dead code, so
        set `place_before` unless the end of the chain is genuinely what you
        want.
        """
        new_id = await router.add(
            *FIREWALL,
            chain=chain, action=action, comment=comment, protocol=protocol,
            src_address=src_address, dst_address=dst_address,
            src_port=src_port, dst_port=dst_port,
            in_interface=in_interface, out_interface=out_interface,
            in_interface_list=in_interface_list, out_interface_list=out_interface_list,
            connection_state=connection_state,
            src_address_list=src_address_list, dst_address_list=dst_address_list,
            log=log, log_prefix=log_prefix, disabled=disabled or None,
        )
        moved_before = None
        if place_before:
            target = _require_real_id(place_before)
            await router.run(*FIREWALL, command="move", numbers=new_id, destination=target)
            moved_before = target

        rule = await router.get(*FIREWALL, item_id=new_id)
        return _dump({"created": True, "id": new_id, "placed_before": moved_before, "rule": rule})

    @mcp.tool(name="update_firewall_rule", annotations=ToolAnnotations(
        title="Update Firewall Rule", destructive_hint=True, idempotent_hint=True, open_world_hint=False))
    async def update_firewall_rule(
        ctx: Context,
        rule_id: Annotated[str, Field(description="The rule 'id' from list_firewall_rules, e.g. '*7'.")],
        chain: str | None = None,
        action: str | None = None,
        comment: str | None = None,
        protocol: str | None = None,
        src_address: str | None = None,
        dst_address: str | None = None,
        src_port: str | None = None,
        dst_port: str | None = None,
        in_interface: str | None = None,
        out_interface: str | None = None,
        in_interface_list: str | None = None,
        out_interface_list: str | None = None,
        connection_state: str | None = None,
        src_address_list: str | None = None,
        dst_address_list: str | None = None,
        log: bool | None = None,
        log_prefix: str | None = None,
    ) -> str:
        """Change fields on an existing firewall rule.

        Only the arguments you pass are modified; omitted ones are left alone.
        Returns the rule as it stands afterwards.
        """
        item_id = _require_real_id(rule_id)
        before = await router.get(*FIREWALL, item_id=item_id)
        if before is None:
            raise ToolError(f"No firewall rule with id {item_id!r} exists. It may have been removed.")

        await router.update(
            *FIREWALL, item_id=item_id,
            chain=chain, action=action, comment=comment, protocol=protocol,
            src_address=src_address, dst_address=dst_address,
            src_port=src_port, dst_port=dst_port,
            in_interface=in_interface, out_interface=out_interface,
            in_interface_list=in_interface_list, out_interface_list=out_interface_list,
            connection_state=connection_state,
            src_address_list=src_address_list, dst_address_list=dst_address_list,
            log=log, log_prefix=log_prefix,
        )
        after = await router.get(*FIREWALL, item_id=item_id)
        return _dump({"updated": True, "id": item_id, "before": before, "after": after})

    @mcp.tool(name="set_firewall_rule_enabled", annotations=ToolAnnotations(
        title="Enable/Disable Firewall Rule", destructive_hint=True, idempotent_hint=True, open_world_hint=False))
    async def set_firewall_rule_enabled(
        ctx: Context,
        rule_id: Annotated[str, Field(description="The rule 'id' from list_firewall_rules, e.g. '*7'.")],
        enabled: bool,
    ) -> str:
        """Enable or disable one firewall rule, leaving its position intact.

        Disabling is the reversible way to test whether a rule is responsible
        for something — prefer it over removing the rule.
        """
        item_id = _require_real_id(rule_id)
        rule = await router.get(*FIREWALL, item_id=item_id)
        if rule is None:
            raise ToolError(f"No firewall rule with id {item_id!r} exists. It may have been removed.")
        await router.update(*FIREWALL, item_id=item_id, disabled=not enabled)
        return _dump({"changed": True, "id": item_id, "enabled": enabled,
                      "rule": await router.get(*FIREWALL, item_id=item_id)})

    @mcp.tool(name="remove_firewall_rule", annotations=ToolAnnotations(
        title="Remove Firewall Rule", destructive_hint=True, idempotent_hint=True, open_world_hint=False))
    async def remove_firewall_rule(
        ctx: Context,
        rule_id: Annotated[str, Field(description="The rule 'id' from list_firewall_rules, e.g. '*7'.")],
        confirm_comment: Annotated[str | None, Field(description="If given, the removal only proceeds when the rule's comment matches. Cheap insurance that you are deleting the rule you think you are.")] = None,
    ) -> str:
        """Delete a firewall rule permanently.

        There is no undo. Disabling a rule (`set_firewall_rule_enabled`) is
        almost always the better first move, and the returned copy of the
        deleted rule is the only record you will have afterwards.
        """
        item_id = _require_real_id(rule_id)
        rule = await router.get(*FIREWALL, item_id=item_id)
        if rule is None:
            raise ToolError(f"No firewall rule with id {item_id!r} exists. It may already have been removed.")
        if confirm_comment is not None and rule.get("comment", "") != confirm_comment:
            raise ToolError(
                f"Refusing to remove {item_id}: its comment is {rule.get('comment', '')!r}, "
                f"not {confirm_comment!r}. Re-list the rules and check the id."
            )
        await router.remove(*FIREWALL, item_id=item_id)
        return _dump({"removed": True, "id": item_id, "deleted_rule": rule})

    @mcp.tool(name="move_firewall_rule", annotations=ToolAnnotations(
        title="Move Firewall Rule", destructive_hint=True, idempotent_hint=False, open_world_hint=False))
    async def move_firewall_rule(
        ctx: Context,
        rule_id: Annotated[str, Field(description="The 'id' of the rule to move.")],
        before_rule_id: Annotated[str | None, Field(description="The 'id' of the rule to place it in front of. Omit to move it to the end of the list.")] = None,
    ) -> str:
        """Reorder a firewall rule relative to another rule.

        Both ends are given by `id` rather than position, so the move means the
        same thing even if the list shifted since you last read it.
        """
        item_id = _require_real_id(rule_id)
        kwargs: dict[str, Any] = {"numbers": item_id}
        if before_rule_id:
            kwargs["destination"] = _require_real_id(before_rule_id)
        await router.run(*FIREWALL, command="move", **kwargs)
        rows = _summarise(await _list(*FIREWALL), RULE_SUMMARY, False)
        return _dump({"moved": True, "id": item_id, "rules": rows})

    # ── NAT ───────────────────────────────────────────────────────────────

    @mcp.tool(name="list_nat_rules", annotations=ToolAnnotations(
        title="List NAT Rules", read_only_hint=True, idempotent_hint=True, open_world_hint=False))
    async def list_nat_rules(
        ctx: Context,
        chain: Annotated[str | None, Field(description="'srcnat' or 'dstnat'.")] = None,
        verbose: bool = False,
    ) -> str:
        """List NAT rules, including port forwards, in evaluation order."""
        rows = _summarise(await _list(*NAT), RULE_SUMMARY, verbose)
        if chain:
            rows = [r for r in rows if r.get("chain") == chain]
        return _dump({"count": len(rows), "rules": rows})

    @mcp.tool(name="add_nat_rule", annotations=ToolAnnotations(
        title="Add NAT Rule", destructive_hint=False, idempotent_hint=False, open_world_hint=False))
    async def add_nat_rule(
        ctx: Context,
        chain: Annotated[Literal["srcnat", "dstnat"], Field(description="'dstnat' for a port forward, 'srcnat' for masquerade.")],
        action: Annotated[Literal["masquerade", "dst-nat", "src-nat", "redirect", "accept", "netmap"], Field(description="'dst-nat' for port forwards, 'masquerade' for outbound NAT.")],
        comment: str | None = None,
        protocol: str | None = None,
        src_address: str | None = None,
        dst_address: str | None = None,
        dst_port: str | None = None,
        in_interface: str | None = None,
        in_interface_list: str | None = None,
        out_interface: str | None = None,
        to_addresses: Annotated[str | None, Field(description="Destination for dst-nat, e.g. '192.168.88.50'.")] = None,
        to_ports: Annotated[str | None, Field(description="Destination port for dst-nat, e.g. '8080'.")] = None,
        disabled: bool = False,
    ) -> str:
        """Add a NAT rule, such as a port forward.

        A port forward also needs the forward chain to permit the traffic;
        check `list_firewall_rules(chain='forward')` for a rule accepting
        `connection-nat-state=dstnat` before concluding it is broken.
        """
        new_id = await router.add(
            *NAT, chain=chain, action=action, comment=comment, protocol=protocol,
            src_address=src_address, dst_address=dst_address, dst_port=dst_port,
            in_interface=in_interface, in_interface_list=in_interface_list,
            out_interface=out_interface, to_addresses=to_addresses, to_ports=to_ports,
            disabled=disabled or None,
        )
        return _dump({"created": True, "id": new_id, "rule": await router.get(*NAT, item_id=new_id)})

    @mcp.tool(name="set_nat_rule_enabled", annotations=ToolAnnotations(
        title="Enable/Disable NAT Rule", destructive_hint=True, idempotent_hint=True, open_world_hint=False))
    async def set_nat_rule_enabled(ctx: Context, rule_id: str, enabled: bool) -> str:
        """Enable or disable one NAT rule."""
        item_id = _require_real_id(rule_id, "NAT rule")
        await router.update(*NAT, item_id=item_id, disabled=not enabled)
        return _dump({"changed": True, "id": item_id, "rule": await router.get(*NAT, item_id=item_id)})

    @mcp.tool(name="remove_nat_rule", annotations=ToolAnnotations(
        title="Remove NAT Rule", destructive_hint=True, idempotent_hint=True, open_world_hint=False))
    async def remove_nat_rule(ctx: Context, rule_id: str) -> str:
        """Delete a NAT rule permanently. Returns the rule that was removed."""
        item_id = _require_real_id(rule_id, "NAT rule")
        rule = await router.get(*NAT, item_id=item_id)
        if rule is None:
            raise ToolError(f"No NAT rule with id {item_id!r} exists.")
        await router.remove(*NAT, item_id=item_id)
        return _dump({"removed": True, "id": item_id, "deleted_rule": rule})

    # ── DHCP, DNS, routing ────────────────────────────────────────────────

    @mcp.tool(name="list_dhcp_leases", annotations=ToolAnnotations(
        title="List DHCP Leases", read_only_hint=True, idempotent_hint=True, open_world_hint=False))
    async def list_dhcp_leases(
        ctx: Context,
        active_only: bool = False,
        search: Annotated[str | None, Field(description="Case-insensitive match against hostname, MAC or address.")] = None,
    ) -> str:
        """List DHCP leases — effectively the inventory of what is on the network."""
        rows = await _list("ip", "dhcp-server", "lease")
        if active_only:
            rows = [r for r in rows if r.get("status") == "bound"]
        if search:
            needle = search.lower()
            rows = [r for r in rows if any(needle in str(r.get(k, "")).lower()
                                           for k in ("host-name", "mac-address", "address", "comment"))]
        return _dump(_summarise(rows, (".id", "address", "mac-address", "host-name", "status",
                                       "expires-after", "dynamic", "disabled", "server", "comment"), False))

    @mcp.tool(name="make_lease_static", annotations=ToolAnnotations(
        title="Make DHCP Lease Static", destructive_hint=False, idempotent_hint=True, open_world_hint=False))
    async def make_lease_static(
        ctx: Context,
        lease_id: Annotated[str, Field(description="The lease 'id' from list_dhcp_leases.")],
        comment: str | None = None,
    ) -> str:
        """Pin a dynamic DHCP lease so the device keeps its address."""
        item_id = _require_real_id(lease_id, "lease")
        await router.run("ip", "dhcp-server", "lease", command="make-static", numbers=item_id)
        if comment:
            await router.update("ip", "dhcp-server", "lease", item_id=item_id, comment=comment)
        return _dump({"changed": True, "lease": await router.get("ip", "dhcp-server", "lease", item_id=item_id)})

    @mcp.tool(name="list_dns_static", annotations=ToolAnnotations(
        title="List Static DNS", read_only_hint=True, idempotent_hint=True, open_world_hint=False))
    async def list_dns_static(ctx: Context) -> str:
        """List static DNS entries and regex overrides."""
        rows = await _list("ip", "dns", "static")
        return _dump(_summarise(rows, (".id", "name", "regexp", "address", "cname", "type", "ttl", "disabled", "comment"), False))

    @mcp.tool(name="add_dns_static", annotations=ToolAnnotations(
        title="Add Static DNS Entry", destructive_hint=False, idempotent_hint=False, open_world_hint=False))
    async def add_dns_static(
        ctx: Context,
        name: Annotated[str | None, Field(description="Hostname to answer for, e.g. 'nas.lan'.")] = None,
        address: Annotated[str | None, Field(description="IPv4 address to return.")] = None,
        cname: Annotated[str | None, Field(description="Return a CNAME instead of an address.")] = None,
        regexp: Annotated[str | None, Field(description="Match hostnames by regex instead of an exact name.")] = None,
        ttl: str = "1d",
        comment: str | None = None,
    ) -> str:
        """Add a static DNS entry."""
        if not name and not regexp:
            raise ToolError("Provide either `name` (an exact hostname) or `regexp` (a pattern).")
        if not address and not cname:
            raise ToolError("Provide either `address` or `cname` for the answer.")
        new_id = await router.add("ip", "dns", "static", name=name, address=address,
                                  cname=cname, regexp=regexp, ttl=ttl, comment=comment)
        return _dump({"created": True, "id": new_id,
                      "entry": await router.get("ip", "dns", "static", item_id=new_id)})

    @mcp.tool(name="remove_dns_static", annotations=ToolAnnotations(
        title="Remove Static DNS Entry", destructive_hint=True, idempotent_hint=True, open_world_hint=False))
    async def remove_dns_static(ctx: Context, entry_id: str) -> str:
        """Delete a static DNS entry."""
        item_id = _require_real_id(entry_id, "DNS entry")
        entry = await router.get("ip", "dns", "static", item_id=item_id)
        if entry is None:
            raise ToolError(f"No static DNS entry with id {item_id!r} exists.")
        await router.remove("ip", "dns", "static", item_id=item_id)
        return _dump({"removed": True, "id": item_id, "deleted_entry": entry})

    @mcp.tool(name="list_routes", annotations=ToolAnnotations(
        title="List Routes", read_only_hint=True, idempotent_hint=True, open_world_hint=False))
    async def list_routes(ctx: Context, active_only: bool = False) -> str:
        """List the IPv4 routing table."""
        rows = await _list("ip", "route")
        if active_only:
            rows = [r for r in rows if r.get("active") in (True, "true", "yes")]
        return _dump(_summarise(rows, (".id", "dst-address", "gateway", "distance", "active",
                                       "static", "dynamic", "disabled", "routing-table", "comment"), False))

    # ── observability ─────────────────────────────────────────────────────

    @mcp.tool(name="get_logs", annotations=ToolAnnotations(
        title="Get Logs", read_only_hint=True, idempotent_hint=True, open_world_hint=False))
    async def get_logs(
        ctx: Context,
        limit: Annotated[int, Field(description="Most recent N entries.", ge=1, le=500)] = 50,
        topic: Annotated[str | None, Field(description="Filter by RouterOS log topic, e.g. 'firewall', 'dhcp', 'system', 'error'.")] = None,
        search: Annotated[str | None, Field(description="Case-insensitive substring match on the message.")] = None,
    ) -> str:
        """Read the device log, most recent last."""
        rows = await _list("log")
        if topic:
            rows = [r for r in rows if topic in str(r.get("topics", ""))]
        if search:
            needle = search.lower()
            rows = [r for r in rows if needle in str(r.get("message", "")).lower()]
        return _dump({"count": len(rows[-limit:]), "entries": [
            {k: r.get(k) for k in ("time", "topics", "message")} for r in rows[-limit:]
        ]})

    @mcp.tool(name="connectivity_check", annotations=ToolAnnotations(
        title="Connectivity Check", read_only_hint=True, idempotent_hint=True, open_world_hint=False))
    async def connectivity_check(ctx: Context) -> str:
        """Confirm the hub can reach and authenticate to this device."""
        try:
            rows = await device.list("system", "identity")
        except RouterError as exc:
            return _dump({"reachable": False, "error": str(exc)})
        return _dump({"reachable": True, "identity": rows[0].get("name") if rows else None})
