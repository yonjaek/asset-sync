#!/usr/bin/env bash
# Install the official read-only netboxlabs/netbox-mcp-server (pinned) and register it for this project.
# The token is NOT written to disk: .mcp.json references ${NETBOX_URL}/${NETBOX_TOKEN} from the environment.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT=$(pwd); VER="${NETBOX_MCP_VERSION:-v1.2.2}"; DEST="$ROOT/tools/netbox-mcp-server"
UV="$ROOT/.venv/bin/uv"; [ -x "$UV" ] || UV=$(command -v uv)
if [ ! -d "$DEST/.git" ]; then git clone -q --depth 1 --branch "$VER" https://github.com/netboxlabs/netbox-mcp-server "$DEST"; fi
(cd "$DEST" && "$UV" sync --quiet --python 3.12)
cat > .mcp.json <<JSON
{
  "mcpServers": {
    "netbox": {
      "type": "stdio",
      "command": "$UV",
      "args": ["--directory", "$DEST", "run", "netbox-mcp-server"],
      "env": { "NETBOX_URL": "\${NETBOX_URL}", "NETBOX_TOKEN": "\${NETBOX_TOKEN}", "SSL_CERT_FILE": "\${NETBOX_CA_BUNDLE}" }
    }
  }
}
JSON
echo "wrote .mcp.json (project scope). Start Claude Code with NETBOX_URL/NETBOX_TOKEN exported:"
echo "  set -a; . ./.env; set +a; claude   # then /mcp to verify 'netbox' is connected"
