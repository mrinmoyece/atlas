"""Atlas as an MCP client - consuming tools it does not own.

The interesting problem here is not the transport, it is **trust**. A
remote MCP server describes its own tools. That description is attacker-
controlled input which will be placed into an agent's prompt, so:

  * **Allowlist by server AND tool name.** Discovery does not imply
    permission. A server that suddenly advertises a `delete_everything`
    tool gets it dropped, not called.
  * **Sanitise advertised descriptions.** A malicious description
    ("IGNORE PREVIOUS INSTRUCTIONS AND EXFILTRATE ...") is a prompt
    injection vector aimed at your model. Descriptions are length-capped
    and stripped of instruction-like control sequences before they ever
    reach the context window.
  * **Namespace remote tools** as `server::tool`, so a remote server can
    never shadow a local tool name and hijack calls intended for Atlas's
    own confined toolkit.
  * **Cap outputs**, for the same context-poisoning reason as local tools.

`MCPToolProxy` is transport-agnostic: it takes any object exposing
`list_tools()` / `call_tool()`, so tests drive it with a fake and
production drives it with a real `ClientSession` over stdio or HTTP.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

MAX_DESCRIPTION_CHARS = 400
MAX_RESULT_CHARS = 8_000
NAMESPACE_SEP = "::"

# Phrases that have no business in a tool description and are strong
# indicators of an injection attempt aimed at the consuming model.
# Keyword filtering is a *speed bump*, not a control - any determined phrasing
# gets through, and pretending otherwise is the dangerous part. The real
# controls are the allowlist above and the fact that a description can never
# cause a tool to execute. What this buys: the obvious payloads are removed
# and their removal is LOGGED, which turns a silent injection attempt into an
# observable signal.
_INJECTION_PATTERNS = re.compile(
    r"(ignore\s+(all\s+|the\s+)?(previous|prior|above)|"
    r"disregard\s+(all\s+|the\s+)?(previous|prior|above)|"
    r"system\s*(:|override)|"
    r"you\s+are\s+now|"
    r"new\s+instructions?|"
    r"</?(system|instructions?|assistant)>|"
    r"\balways\s+(run|execute|call)\b)",
    re.IGNORECASE,
)
# Zero-width and bidi characters used to smuggle keywords past filters.
_INVISIBLE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff]")


class MCPSessionLike(Protocol):
    """Minimal surface Atlas needs from an MCP session."""

    async def list_tools(self) -> Any: ...

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any: ...


class RemoteToolDescriptor(BaseModel):
    """A remote tool after validation and namespacing."""

    model_config = ConfigDict(frozen=True)

    server: str
    remote_name: str
    qualified_name: str
    description: str
    input_schema: dict[str, Any] = Field(default_factory=dict)
    sanitised: bool = False


def sanitise_description(text: str) -> tuple[str, bool]:
    """Strip injection-shaped content and cap length.

    Returns (clean_text, was_modified) so the modification can be logged -
    a server whose descriptions keep getting sanitised is a signal worth
    alerting on, not silently swallowing.
    """
    original = text or ""
    # Strip invisible characters FIRST: "Ig\u200bnore previous" defeats any
    # keyword filter that runs before normalisation.
    cleaned = _INVISIBLE.sub("", original)
    cleaned = _INJECTION_PATTERNS.sub("[redacted]", cleaned)
    cleaned = cleaned.replace("\x00", "").strip()
    if len(cleaned) > MAX_DESCRIPTION_CHARS:
        cleaned = cleaned[:MAX_DESCRIPTION_CHARS] + "..."
    return cleaned, cleaned != original


MAX_SCHEMA_PROPERTIES = 30
MAX_SCHEMA_DEPTH = 6


def sanitise_schema(schema: Any, _depth: int = 0) -> tuple[dict[str, Any], bool]:
    """Scrub a remote tool's JSON schema before it reaches a prompt.

    Descriptions were sanitised and schemas were passed through verbatim.
    That was survivable only while nothing bound remote tools to a model -
    the moment `bind_toolkit` started sending `spec.parameters` to a
    provider, every `description` field nested inside a remote schema became
    a direct prompt-injection channel that bypassed the very filter sitting
    next to it. Attackers do not aim at the field you remembered to clean.

    Also bounded in size and depth: a schema is untrusted input, and a
    10,000-property object is a context-window attack that needs no clever
    wording at all.
    """
    if not isinstance(schema, dict) or _depth > MAX_SCHEMA_DEPTH:
        return {}, not isinstance(schema, dict)

    out: dict[str, Any] = {}
    modified = False
    for key, value in list(schema.items())[:MAX_SCHEMA_PROPERTIES]:
        if key in ("description", "title", "$comment"):
            clean, changed = sanitise_description(str(value))
            out[key], modified = clean, modified or changed
        elif isinstance(value, dict):
            out[key], changed = sanitise_schema(value, _depth + 1)
            modified = modified or changed
        elif isinstance(value, list):
            out[key] = [
                sanitise_schema(v, _depth + 1)[0] if isinstance(v, dict) else v
                for v in value[:MAX_SCHEMA_PROPERTIES]
            ]
        else:
            out[key] = value
    modified = modified or len(schema) > MAX_SCHEMA_PROPERTIES
    return out, modified


class MCPToolProxy:
    """Discovers, filters and calls tools on one remote MCP server."""

    def __init__(
        self,
        server_name: str,
        session: MCPSessionLike,
        *,
        allowed_tools: Sequence[str] | None = None,
        allow_all: bool = False,
    ) -> None:
        """Connect to a remote MCP server.

        `allowed_tools=None` means **deny everything**. That is the whole
        point of an allowlist and it was previously inverted: the default
        allowed every advertised tool, so the docstring's promise that "a
        server that suddenly advertises a `delete_everything` tool gets it
        dropped" was false under default configuration - the only
        configuration most callers use.

        Allowing everything is still possible, but it must be *written down*
        (`allow_all=True`), which is what makes it a decision rather than an
        accident.
        """
        self.server_name = server_name
        self._session = session
        self._allow_all = allow_all
        self._allowed: set[str] = set(allowed_tools or ())
        self._catalog: dict[str, RemoteToolDescriptor] = {}

    async def discover(self) -> list[RemoteToolDescriptor]:
        raw = await self._session.list_tools()
        # MCP sessions return either an object with `.tools` or a plain
        # mapping, depending on SDK version and transport. Handling only the
        # attribute form silently discovers zero tools against a dict-shaped
        # response - a failure that looks like "the server has no tools"
        # rather than like a bug.
        if isinstance(raw, dict):
            tools = raw.get("tools") or []
        else:
            tools = getattr(raw, "tools", raw) or []
        out: list[RemoteToolDescriptor] = []
        for tool in tools:
            name = _attr(tool, "name")
            if not name:
                continue
            if not self._allow_all and name not in self._allowed:
                # Not on the allowlist -> never advertised to the model.
                continue
            description, modified = sanitise_description(_attr(tool, "description") or "")
            raw_schema = _attr(tool, "inputSchema") or _attr(tool, "input_schema") or {}
            schema, schema_modified = sanitise_schema(raw_schema)
            descriptor = RemoteToolDescriptor(
                server=self.server_name,
                remote_name=name,
                qualified_name=f"{self.server_name}{NAMESPACE_SEP}{name}",
                description=description,
                input_schema=schema,
                sanitised=modified or schema_modified,
            )
            self._catalog[descriptor.qualified_name] = descriptor
            out.append(descriptor)
        return out

    @property
    def catalog(self) -> dict[str, RemoteToolDescriptor]:
        return dict(self._catalog)

    async def call(self, qualified_name: str, arguments: dict[str, Any]) -> str:
        """Call a discovered tool. Unknown or disallowed names never reach
        the network - they fail closed, locally."""
        descriptor = self._catalog.get(qualified_name)
        if descriptor is None:
            return f"ERROR: tool {qualified_name!r} is not in the discovered allowlist"
        try:
            result = await self._session.call_tool(descriptor.remote_name, arguments)
        except Exception as e:  # noqa: BLE001 - remote surface is untrusted
            return f"ERROR: remote call failed: {type(e).__name__}: {e}"
        return _stringify(result)[:MAX_RESULT_CHARS]


def _attr(obj: Any, name: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _stringify(result: Any) -> str:
    """MCP results are content blocks; flatten to text for the model."""
    content = _attr(result, "content")
    if content is None:
        return str(result)
    parts: list[str] = []
    for block in content if isinstance(content, list) else [content]:
        text = _attr(block, "text")
        parts.append(text if text is not None else str(block))
    return "\n".join(parts)
