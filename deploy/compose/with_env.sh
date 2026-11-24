#!/bin/sh
# Run a command with this install's settings loaded from the state volume.
#   with_env.sh <command> [args…]
# Used by the init step and the engine in docker-compose.yml. The settings are
# made by deploy/compose/setup.py; they are read here, at start, because they
# do not exist yet when compose reads its own file.
set -eu
STATE="${ENGINE_STATE_DIR:-/run/engine}"
if [ ! -f "$STATE/engine.env" ]; then
  echo "[engine] $STATE/engine.env is missing: the setup step has not run" >&2
  exit 1
fi

# Model keys passed through compose arrive as empty strings when the host has
# none set. An empty key is not "no key" to every library, so drop them.
for name in GOOGLE_API_KEY OPENAI_API_KEY ANTHROPIC_API_KEY AZURE_OPENAI_API_KEY \
            AZURE_OPENAI_ENDPOINT PORTAL_NAME \
            EMBED_PROVIDER EMBED_BASE_URL EMBED_MODEL EMBED_API_KEY \
            VOICE_LIVE_SIM_ENABLED; do
  eval "value=\${$name:-}"
  if [ -z "$value" ]; then unset "$name"; fi
done

# The setup step allows agents to use MCP servers and HTTP tools on private
# addresses, which suits one machine. A value given to the container wins, so
# a server on a shared network can turn it off (MCP_ALLOW_LOCAL_ENDPOINTS=0).
allow_local="${MCP_ALLOW_LOCAL_ENDPOINTS:-}"

set -a
. "$STATE/engine.env"
set +a
if [ -n "$allow_local" ]; then
  MCP_ALLOW_LOCAL_ENDPOINTS="$allow_local"
  export MCP_ALLOW_LOCAL_ENDPOINTS
fi
exec "$@"
