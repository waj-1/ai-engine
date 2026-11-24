#!/bin/sh
# Start the engine where nothing orders the services' start (one host, no
# compose: docs/deploy/demo.md, "One host, no compose").
#
#   1. wait for this install's settings (the setup step writes engine.env)
#   2. run the init step (deploy/compose/init.py): it waits up to INIT_WAIT_SEC
#      for the auth service and the REST layer, then applies the schema
#   3. exec uvicorn
#
# The engine seeds what it needs as it starts (and, with DEMO_MODE on, the
# demo account and the Northwind demo: demo_mode/reset.py on_boot), so there is
# no separate seed step. Exits non-zero when a step fails, for the platform to
# restart the container.
set -eu
STATE="${ENGINE_STATE_DIR:-/run/engine}"
WAIT="${INIT_WAIT_SEC:-300}"
HERE="$(cd "$(dirname "$0")" && pwd)"

waited=0
while [ ! -f "$STATE/engine.env" ]; do
  if [ "$waited" -ge "$WAIT" ]; then
    echo "[engine] $STATE/engine.env did not appear in ${WAIT}s: has the setup step run?" >&2
    exit 1
  fi
  sleep 2
  waited=$((waited + 2))
done

export INIT_WAIT_SEC="$WAIT"
sh "$HERE/with_env.sh" python "$HERE/init.py"
exec sh "$HERE/with_env.sh" sh -c \
  'exec uvicorn main:app --host 0.0.0.0 --port "${PORT:-8000}" --workers 1 --proxy-headers --forwarded-allow-ips="${FORWARDED_ALLOW_IPS:-127.0.0.1}"'
