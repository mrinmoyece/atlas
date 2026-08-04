"""Model Context Protocol: Atlas as an MCP *server* and as an MCP *client*.

Most projects implement one side. Implementing both is what demonstrates
that you understand MCP as a protocol rather than as an SDK call:

    server.py  exposes Atlas's repository tools so any MCP host (Claude
               Desktop, an IDE, another agent) can use them.
    client.py  consumes third-party MCP servers so Atlas's agents can use
               tools it does not own - with an allowlist, because a remote
               server is untrusted code describing itself.
"""

from atlas.mcp_layer.client import MCPToolProxy, RemoteToolDescriptor
from atlas.mcp_layer.server import build_mcp_server, mcp_tool_definitions

__all__ = [
    "build_mcp_server",
    "mcp_tool_definitions",
    "MCPToolProxy",
    "RemoteToolDescriptor",
]
