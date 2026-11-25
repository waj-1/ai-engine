"""Run a container-group spec (aci_spec_tracing.py's JSON) on this machine with Docker.

    SPEC_DRY_RUN=1 OUT=spec.json python3 deploy/demo/aci_spec_tracing.py
    python3 deploy/demo/run_spec_locally.py spec.json up   --engine-image my-engine:dev \\
        [--env NAME=VALUE ...] [--mount /abs/checkout:/app] [--port 18080]
    python3 deploy/demo/run_spec_locally.py spec.json down

A rehearsal of the group before a deploy: every container shares ONE network
namespace (they reach each other on 127.0.0.1, and two that want the same port
collide here exactly as they would there), a container's `command` replaces
the image's entrypoint, volumes are empty at the start, the init container
runs to completion first, and a container that exits is started again.

Differences from the platform: images are pulled from their public names (the
registry prefix is dropped; `--engine-image` names the engine's), `--env`
adds or replaces engine settings (a local model instead of the cloud one),
and only the first published port is mapped, to 127.0.0.1:<--port>.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys

PREFIX = "specsim"
PUBLIC = {"supabase/postgres": "ghcr.io/supabase/postgres", "supabase/gotrue": "ghcr.io/supabase/gotrue",
          "postgrest/postgrest": "postgrest/postgrest", "kong": "kong",
          "clickhouse/clickhouse-server": "clickhouse/clickhouse-server",
          "lmnr-ai/app-server": "ghcr.io/lmnr-ai/app-server", "lmnr-ai/frontend": "ghcr.io/lmnr-ai/frontend"}


def docker(*args: str, check: bool = True) -> str:
    out = subprocess.run(["docker", *args], capture_output=True, text=True)
    if check and out.returncode:
        raise SystemExit(f"docker {' '.join(args[:3])}…: {out.stderr.strip()}")
    return out.stdout.strip()


def local_image(image: str, engine_image: str) -> str:
    repo, _, tag = image.split("/", 1)[1].rpartition(":")
    if repo not in PUBLIC:
        return engine_image
    return f"{PUBLIC[repo]}:{tag}"


def run_args(name: str, props: dict, volumes: dict, engine_image: str, extra_env: dict, mount: str | None) -> list:
    image = local_image(props["image"], engine_image)
    args = ["--name", f"{PREFIX}-{name}", "--network", f"container:{PREFIX}-pod"]
    env = {e["name"]: e.get("value", e.get("secureValue", "")) for e in props.get("environmentVariables", [])}
    if image == engine_image:
        env.update(extra_env)
        if mount:
            args += ["-v", mount + ":ro"]
    for key, value in env.items():
        args += ["-e", f"{key}={value}"]
    for m in props.get("volumeMounts", []):
        args += ["-v", f"{volumes[m['name']]}:{m['mountPath']}" + (":ro" if m.get("readOnly") else "")]
    command = props.get("command") or []
    if command:
        args += ["--entrypoint", command[0]]
    return args + [image] + command[1:]


def up(spec: dict, a) -> None:
    props = spec["properties"]
    port = props["ipAddress"]["ports"][0]["port"]
    extra = dict(item.split("=", 1) for item in a.env)
    volumes = {v["name"]: f"{PREFIX}-{v['name']}" for v in props["volumes"]}
    for vol in volumes.values():
        docker("volume", "create", vol)
    docker("run", "-d", "--name", f"{PREFIX}-pod", "-p", f"127.0.0.1:{a.port}:{port}",
           "--add-host", "host.docker.internal:host-gateway", "registry.k8s.io/pause:3.9")
    for init in props.get("initContainers", []):
        # Init containers run as the platform runs them: root, to completion.
        print("init:", init["name"], flush=True)
        docker("run", "--user", "0", *run_args(init["name"], init["properties"], volumes, a.engine_image, extra, a.mount))
    for cont in props["containers"]:
        print("start:", cont["name"], flush=True)
        docker("run", "-d", "--restart", "always",
               *run_args(cont["name"], cont["properties"], volumes, a.engine_image, extra, a.mount))
    print(f"up: http://127.0.0.1:{a.port}  (docker logs -f {PREFIX}-engine)")


def down(spec: dict) -> None:
    names = docker("ps", "-aq", "--filter", f"name=^{PREFIX}-", check=False).split()
    if names:
        docker("rm", "-f", *names, check=False)
    for v in spec["properties"]["volumes"]:
        docker("volume", "rm", f"{PREFIX}-{v['name']}", check=False)
    print("down")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("spec")
    ap.add_argument("action", choices=("up", "down"))
    ap.add_argument("--engine-image", default="ai-engine:dev")
    ap.add_argument("--env", action="append", default=[])
    ap.add_argument("--mount")
    ap.add_argument("--port", type=int, default=18080)
    a = ap.parse_args()
    spec = json.load(open(a.spec))
    down(spec) if a.action == "down" else up(spec, a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
