"""MCP client runtime: transports, discovery, tools and resources."""

# Keep package initialization dependency-free.  The configuration layer uses
# xg.mcp.models and must not recursively import the runtime manager.
