"""A long-lived client session against an upstream MCP server.

The transport is an async context manager that owns a task group internally, so
it has to be entered and exited in the same task. A dedicated worker task holds
it open; everyone else waits for `_ready` and then uses the session object,
which is safe to drive concurrently because JSON-RPC ids keep responses apart.

The alternative — a fresh session per tool call — would mean a connect and an
`initialize` round trip before every single call.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import Prompt, Resource, Tool

log = logging.getLogger(__name__)


class UpstreamError(RuntimeError):
    """An upstream failure worth showing the user verbatim."""


@dataclass(frozen=True)
class UpstreamConfig:
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    timeout: float = 30.0
    verify_tls: bool = True


class Upstream:
    """One reusable connection to an upstream MCP server."""

    def __init__(self, config: UpstreamConfig) -> None:
        self._cfg = config
        self._session: ClientSession | None = None
        self._worker: asyncio.Task[None] | None = None
        self._ready = asyncio.Event()
        self._stop = asyncio.Event()
        self._failure: BaseException | None = None
        self._lock = asyncio.Lock()

    # ── lifecycle ─────────────────────────────────────────────────────────

    async def _run(self) -> None:
        try:
            client = httpx2.AsyncClient(
                headers=self._cfg.headers,
                timeout=self._cfg.timeout,
                verify=self._cfg.verify_tls,
            )
            async with client:
                async with streamable_http_client(self._cfg.url, http_client=client) as streams:
                    read, write = streams[0], streams[1]
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        self._session = session
                        self._ready.set()
                        await self._stop.wait()
        except BaseException as exc:  # noqa: BLE001 - re-raised to every waiter
            self._failure = exc
            log.warning("upstream %s failed: %s", self._cfg.url, exc)
        finally:
            self._session = None
            # Wake anyone still waiting; they check _failure and report it.
            self._ready.set()

    async def session(self) -> ClientSession:
        async with self._lock:
            if self._session is not None:
                return self._session
            if self._worker is None or self._worker.done():
                self._failure = None
                self._ready = asyncio.Event()
                self._stop = asyncio.Event()
                self._worker = asyncio.create_task(self._run())

        try:
            await asyncio.wait_for(self._ready.wait(), timeout=self._cfg.timeout + 5)
        except asyncio.TimeoutError as exc:
            raise UpstreamError(f"Timed out connecting to {self._cfg.url}") from exc

        if self._session is None:
            raise UpstreamError(_explain(self._cfg.url, self._failure))
        return self._session

    async def close(self) -> None:
        self._stop.set()
        worker, self._worker = self._worker, None
        if worker is not None:
            try:
                await asyncio.wait_for(asyncio.shield(worker), timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                worker.cancel()
            except Exception:  # noqa: BLE001 - shutting down
                pass
        self._session = None

    async def _retrying(self, fn_name: str, *args: Any, **kwargs: Any) -> Any:
        """Run one call, rebuilding the session once if it has gone away."""
        for attempt in (1, 2):
            session = await self.session()
            try:
                return await getattr(session, fn_name)(*args, **kwargs)
            except UpstreamError:
                raise
            except Exception as exc:  # noqa: BLE001 - transport-level failure
                await self.close()
                if attempt == 2:
                    raise UpstreamError(_explain(self._cfg.url, exc)) from exc
                log.info("upstream %s dropped, reconnecting (%s)", self._cfg.url, exc)
        raise AssertionError("unreachable")

    # ── introspection and forwarding ──────────────────────────────────────

    async def list_tools(self) -> list[Tool]:
        return (await self._retrying("list_tools")).tools

    async def list_prompts(self) -> list[Prompt]:
        try:
            return (await self._retrying("list_prompts")).prompts
        except Exception:  # noqa: BLE001 - upstream may not implement prompts
            return []

    async def list_resources(self) -> list[Resource]:
        try:
            return (await self._retrying("list_resources")).resources
        except Exception:  # noqa: BLE001 - upstream may not implement resources
            return []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        return await self._retrying("call_tool", name, arguments)

    async def get_prompt(self, name: str, arguments: dict[str, str] | None = None) -> Any:
        return await self._retrying("get_prompt", name, arguments)

    async def read_resource(self, uri: Any) -> Any:
        return await self._retrying("read_resource", uri)

    async def server_info(self) -> dict[str, Any]:
        session = await self.session()
        # `initialize()` caches its result and returns it again on a second call,
        # so this reads the handshake rather than repeating it.
        result = await session.initialize()
        info = result.server_info
        return {
            "name": info.name if info else None,
            "version": info.version if info else None,
            "instructions": result.instructions,
        }


def _explain(url: str, exc: BaseException | None) -> str:
    """Turn a transport failure into something worth putting in front of a user."""
    if exc is None:
        return f"Could not connect to {url}."
    text = str(exc) or type(exc).__name__
    if isinstance(exc, httpx2.HTTPStatusError) and exc.response.status_code in (401, 403):
        return (
            f"{url} refused the request ({exc.response.status_code}). "
            "The upstream server wants credentials — set an auth header on this backend."
        )
    if isinstance(exc, httpx2.ConnectError):
        return f"Could not reach {url}: {text}. Check the URL, port and that the server is running."
    return f"{url}: {text}"
