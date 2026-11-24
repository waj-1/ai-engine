# Run WAJ AI Engine on Coolify

> **Status: validated against Coolify's documentation, not yet deployed by us.**
> We ran this exact compose file with plain Docker Compose on a Linux machine
> (the stack came up healthy and the first-run page opened). We have not run
> it on a Coolify server. If a step below is wrong, please open an issue.

[Coolify](https://coolify.io) deploys a Docker Compose file to your own
server and puts its HTTPS proxy in front. The file for it is
[`deploy/coolify/waj-ai-engine.yaml`](../../deploy/coolify/waj-ai-engine.yaml).

## Why not the `docker-compose.yml` at the repository root

That file is made for one machine and `localhost`. Three things in it do not
fit Coolify:

| Root `docker-compose.yml` | `deploy/coolify/waj-ai-engine.yaml` |
|---|---|
| Publishes ports on `127.0.0.1` only | Publishes no host port. Coolify's proxy routes the domain to the engine on port 8000 |
| Mounts two files from the repository | Mounts nothing from the repository. The setup step writes those files into a volume |
| Builds the engine image when it cannot pull it | Pulls `ghcr.io/waj-1/ai-engine` only |

Everything else is the same: the same seven steps, the same images, the same
secrets made on the first start and kept in a volume.

## What you need

- A server managed by Coolify v4, `linux/amd64`. 2 vCPU and 4 GB of memory is
  a comfortable start.
- A published engine image. The file pulls release `0.1.1` unless you set
  `AI_ENGINE_VERSION`.
- A domain for the engine, or a wildcard domain set on the Coolify server.

## Deploy from the Git repository

1. In your Coolify project choose **New resource**, then **Public repository**.
2. Repository URL: `https://github.com/waj-1/ai-engine`. Branch: `main`.
3. Build pack: **Docker Compose**.
4. Base directory: `/`. Docker Compose location:
   `/deploy/coolify/waj-ai-engine.yaml`.
5. Continue. Coolify lists the services it found in the file.
6. Set the domain on the **engine** service. With a wildcard domain on the
   server, Coolify fills one in from `SERVICE_URL_ENGINE_8000`. Without one,
   type your own in the form `https://engine.example.com:8000`. The `:8000`
   tells Coolify which port inside the container to route to. Visitors still
   use the normal HTTPS port.
7. Optional: fill in settings under **Environment variables** (see below).
8. **Deploy**.

## Or paste the file

Choose **New resource**, then **Docker Compose Empty**, and paste the content
of `deploy/coolify/waj-ai-engine.yaml`. Steps 6 to 8 are the same. Nothing
else from the repository is needed.

## First login

There is no default login. Open the engine's domain. The first-run page asks
for a one-time setup code and creates the admin account. The code is in the
**engine** service's logs in Coolify (look for `Setup code`).

To skip the browser step, set both `ADMIN_EMAIL` and `ADMIN_PASSWORD` before
the first deploy. Setting only one stops the setup step with a message.

## Settings

All are optional. Set them under **Environment variables**, then redeploy.

| Setting | Effect |
|---|---|
| `AI_ENGINE_VERSION` | The engine release to run. Default `0.1.1` |
| `ADMIN_EMAIL`, `ADMIN_PASSWORD` | Create the admin account at the first deploy (set both) |
| `GOOGLE_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_ENDPOINT` | A model provider key from the environment. A key saved in the portal under **Settings › Model keys** wins |
| `EMBED_MODEL`, `EMBED_BASE_URL`, `EMBED_API_KEY`, `EMBED_PROVIDER` | An embedding model for knowledge bases |
| `PORTAL_NAME` | The name shown in the portal |

## What is stored where

| Volume | Holds |
|---|---|
| `engine-db` | The database |
| `engine-state` | This install's secrets: database password, signing secret, `SECRET_KEY` |

Coolify adds the resource's id to the volume names. Back up both together.
Model keys saved in the portal are encrypted with the `SECRET_KEY` in
`engine-state`, so a database backup alone cannot be restored.

Deleting the resource with its volumes deletes the database and the secrets.

## Upgrade

Set `AI_ENGINE_VERSION` to the new release and redeploy. The engine applies
new database migrations when it starts.

## Good to know

- `setup` runs once per deploy and exits. The file marks it with
  `exclude_from_hc: true` so Coolify does not count the exited container as
  unhealthy. That key is Coolify's own: plain `docker compose` rejects it.
- Only the engine has a domain. The database, the REST layer, the auth
  service and the gateway are reachable inside the stack's network only.
- `MCP_ALLOW_LOCAL_ENDPOINTS` is off in this file, so an agent's tools cannot
  be pointed at addresses inside your server's network. Set it to `1` in the
  service's environment variables if you want agents to use an MCP server or
  an HTTP tool on the host or its private network. Admins of the install can then
  reach those addresses, so give admin accounts only to people you trust.
- An Ollama server on the Coolify host is reachable from the engine at
  `http://host.docker.internal:11434`. Model addresses are not affected by
  that setting.

## Getting into Coolify's one-click catalog

The same file is written as a catalog template: it starts with the header
lines Coolify reads (`documentation`, `slogan`, `category`, `tags`, `logo`,
`port`), uses no build, pins every image and declares its volumes.

It has not been submitted. As of November 2026 Coolify's contribution guide
says two things that block it:

- External template contributions are paused, with no date for reopening.
- A service needs at least 1,000 GitHub stars to be added.

When both are cleared, the steps are:

1. Add a square logo as `svgs/waj-ai-engine.svg` in a fork of
   `coollabsio/coolify`. This repository has no SVG logo yet.
2. Copy `deploy/coolify/waj-ai-engine.yaml` to
   `templates/compose/waj-ai-engine.yaml` in that fork. Replace
   `${AI_ENGINE_VERSION:-0.1.1}` with the fixed release number if reviewers
   ask for a plain tag.
3. Test it on a Coolify server through **Docker Compose Empty**, from a fresh
   resource.
4. Open the pull request against the current `release/v4.x` branch, and the
   matching documentation pull request in `coollabsio/coolify-docs`. Link the
   two.
