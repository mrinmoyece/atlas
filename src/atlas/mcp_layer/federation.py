"""Federating remote MCP tools onto the local toolkit.

This module is what turns "Atlas is an MCP client" from a true statement
about a class that exists into a true statement about the running system.
`MCPToolProxy` was fully implemented and fully tested, and nothing in the
agent path ever called it — the README said Atlas consumed remote tools, and
a reviewer checking imports would have found the client reachable only from
the test suite and the demo. A capability that no agent can reach is a
capability the system does not have.

Three properties this layer must preserve, because federation is where a
carefully confined tool surface usually springs a leak:

  1. **Local tools always win.** Remote names are namespaced `server::tool`
     by `MCPToolProxy`, so a remote server cannot shadow `read_file` and
     silently receive calls meant for the confined local implementation.
     This class additionally refuses to register a remote tool whose
     qualified name collides with a local one — belt and braces, because the
     namespacing lives in a different module and could be relaxed there
     without anyone noticing here.

  2. **Failures become observations, not crashes.** Identical contract to
     `RepoToolkit.call`: every error path returns an `ERROR: ...` string the
     model can read and react to. A remote server that times out, returns
     garbage, or disappears mid-run must not take down the specialist — the
     other three specialists are still doing useful work.

  3. **Remote output is bounded.** Remote servers are not trusted to respect
     any size limit, and an unbounded tool result blows the context window
     of whichever specialist called it.

The sync/async bridge is deliberate. Patterns are synchronous by design (the
reasoning loop reads much better as straight-line code), MCP sessions are
async, and each specialist already runs on its own thread. So each call gets
its own event loop, which is cheap relative to a network round trip and
avoids the "attached to a different loop" failure that comes from caching a
loop across threads.
"""

from __future__ import annotations

import asyncio
from typing import Any

from atlas.mcp_layer.client import MAX_RESULT_CHARS, NAMESPACE_SEP, MCPToolProxy
from atlas.tools.repo import RepoToolkit, ToolSpec, tool_specs


class FederatedToolkit:
    """A `RepoToolkit` plus zero or more remote MCP servers.

    Deliberately duck-typed rather than a subclass: `PatternContext` accepts
    anything with `.call(name, arguments)`, and inheriting from
    `RepoToolkit` would imply this class is confined to a repository root,
    which is exactly the thing federation stops being true.
    """

    def __init__(
        self,
        local: RepoToolkit,
        proxies: tuple[MCPToolProxy, ...] = (),
        *,
        discovery_timeout_s: float = 5.0,
        call_timeout_s: float = 15.0,
    ) -> None:
        self.local = local
        self.proxies = proxies
        self._discovery_timeout_s = discovery_timeout_s
        self._call_timeout_s = call_timeout_s
        self._remote: dict[str, MCPToolProxy] = {}
        self._remote_specs: list[ToolSpec] = []
        self._local_names = set(local.as_callables())
        self._discovery_errors: dict[str, str] = {}

    # -- discovery -----------------------------------------------------

    def discover(self) -> list[ToolSpec]:
        """Ask every remote server what it offers; never raise.

        A remote server being down is an operational event, not a reason to
        fail the analysis: Atlas's own toolkit is sufficient for a complete
        report, and remote tools are an enhancement. The error is recorded
        so `discovery_errors` can surface it rather than being swallowed.
        """
        self._remote.clear()
        self._remote_specs.clear()
        self._discovery_errors.clear()

        for proxy in self.proxies:
            try:
                descriptors = self._run(proxy.discover(), timeout=self._discovery_timeout_s)
            except Exception as exc:  # noqa: BLE001 - untrusted remote
                self._discovery_errors[proxy.server_name] = f"{type(exc).__name__}: {exc}"
                continue

            for descriptor in descriptors:
                name = descriptor.qualified_name
                if NAMESPACE_SEP not in name or name in self._local_names:
                    # Cannot happen with the current client, which is the
                    # point: if someone relaxes namespacing over there, the
                    # shadowing attack fails here instead of succeeding
                    # silently.
                    self._discovery_errors[proxy.server_name] = (
                        f"refused tool {name!r}: would shadow a local tool"
                    )
                    continue
                self._remote[name] = proxy
                self._remote_specs.append(
                    ToolSpec(
                        name=name,
                        description=descriptor.description,
                        parameters=descriptor.input_schema,
                    )
                )
        return list(self._remote_specs)

    @property
    def discovery_errors(self) -> dict[str, str]:
        return dict(self._discovery_errors)

    def specs(self) -> list[ToolSpec]:
        """Local tools first, so a model scanning the list sees the confined,
        deterministic ones before anything federated."""
        return [*tool_specs(), *self._remote_specs]

    def as_callables(self) -> dict[str, Any]:
        return self.local.as_callables()

    # -- dispatch ------------------------------------------------------

    def call(self, name: str, arguments: dict[str, Any]) -> str:
        """Local first, then remote. Same error contract as `RepoToolkit`."""
        if name in self._local_names:
            return self.local.call(name, arguments)

        proxy = self._remote.get(name)
        if proxy is None:
            # Falls through to the local dispatcher so the "unknown tool"
            # message is worded identically whether or not federation is on.
            return self.local.call(name, arguments)

        try:
            result = self._run(proxy.call(name, arguments), timeout=self._call_timeout_s)
        except (asyncio.TimeoutError, TimeoutError):
            # Both, deliberately. `asyncio.TimeoutError` only became an alias
            # for the builtin in Python 3.11; on 3.10 - which this project
            # supports and CI runs - they are distinct classes, so catching
            # the builtin alone let every timeout fall through to the generic
            # handler and report as a mystery failure.
            return f"ERROR: remote tool {name!r} timed out after {self._call_timeout_s}s"
        except Exception as exc:  # noqa: BLE001 - untrusted remote
            return f"ERROR: remote tool {name!r} failed: {type(exc).__name__}: {exc}"

        text = result if isinstance(result, str) else str(result)
        return text[:MAX_RESULT_CHARS]

    # -- internals -----------------------------------------------------

    @staticmethod
    def _run(coro: Any, *, timeout: float) -> Any:
        """Run one coroutine to completion on a private event loop.

        `asyncio.run` would refuse if a loop is already running on this
        thread (the API's streaming path), and reusing a cached loop across
        specialist threads produces "future attached to a different loop".
        A fresh loop per call sidesteps both and costs microseconds next to
        a network round trip.
        """
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(asyncio.wait_for(coro, timeout=timeout))
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            finally:
                loop.close()


def federate(
    local: RepoToolkit, proxies: tuple[MCPToolProxy, ...] = ()
) -> RepoToolkit | FederatedToolkit:
    """Return the local toolkit unchanged when there is nothing to federate.

    Keeps the common path free of a wrapper that would only forward calls,
    so the traceback from a tool bug in the default configuration points
    straight at `RepoToolkit`.
    """
    if not proxies:
        return local
    toolkit = FederatedToolkit(local, proxies)
    toolkit.discover()
    return toolkit
