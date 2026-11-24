# Run WAJ AI Engine on Render

> **Status: validated against Render's Blueprint specification, not yet
> deployed by us.** `render.yaml` passes Render's published JSON schema. We
> ran the same five services, wired the same way (secrets as environment
> variables, no shared volume, no start order), with Docker Compose on a
> Linux machine: the stack came up healthy and the admin sign-in worked. We
> have not run the Blueprint on Render. If a step below is wrong, please open
> an issue.

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/waj-1/ai-engine)

The button reads [`render.yaml`](../../render.yaml) from this repository and
creates everything in it.

## What it creates

Five services in one region, on Render's private network:

| Service | Type | Runs | Plan |
|---|---|---|---|
| `ai-engine` | Web service (public) | The engine and its admin portal, from `ghcr.io/waj-1/ai-engine:latest` | `standard` |
| `ai-engine-gateway` | Private service | One address in front of the next two, built from `deploy/paas/gateway.Dockerfile` | `starter` |
| `ai-engine-rest` | Private service | The REST layer, `postgrest/postgrest` | `starter` |
| `ai-engine-auth` | Private service | The auth service, `supabase/gotrue` | `starter` |
| `ai-engine-db` | Private service | Postgres with pgvector, built from `deploy/paas/db.Dockerfile`, with a 10 GB disk | `standard` |

All five are paid instances, and the disk is billed too. A persistent disk
and private services are not available on Render's free plan. Check Render's
pricing page before you deploy. You can change a plan in the dashboard later.

Why not Render's managed Postgres: the engine reaches its data through the
REST layer and the auth service, and those need database roles that only a
superuser can create.

## Secrets

`render.yaml` holds no secret. Render generates three when the Blueprint is
first applied and keeps them on later syncs:

| Secret | Kept on | Used for |
|---|---|---|
| `POSTGRES_PASSWORD` | `ai-engine-db` | The database password |
| `GOTRUE_JWT_SECRET` | `ai-engine-auth` | Signing every access token |
| `SECRET_KEY` | `ai-engine` | Encrypting model keys saved in the portal |

The other services read them by reference. The engine signs its two API
tokens from the signing secret each time it starts, so no token is stored.

This is different from `docker compose up`, where a setup step writes the
secrets into a shared Docker volume. Render services cannot share a disk, so
here the platform makes the secrets and
[`deploy/paas/start_engine.py`](../../deploy/paas/start_engine.py) turns them
into the engine's settings.

## Deploy

1. Click the button above, or open
   `https://render.com/deploy?repo=https://github.com/waj-1/ai-engine`.
2. Sign in to Render and give the Blueprint a name.
3. **Apply**. Render builds two small images (database, gateway), pulls the
   other three and starts all five.
4. Wait for `ai-engine` to show **Live**. The first deploy takes several
   minutes: the engine waits for the database and the gateway to be built,
   then applies the database schema.

The auth service exits while the database is not up yet. Render restarts it
and it then stays up. That is expected on the first deploy.

## First login

There is no default login. Open the `ai-engine` service's URL
(`https://ai-engine-….onrender.com`). The first-run page asks for a one-time
setup code and creates the admin account. The code is in the `ai-engine`
service's **Logs** (look for `Setup code`).

To skip the browser step, add both `ADMIN_EMAIL` and `ADMIN_PASSWORD` to the
`ai-engine` service's environment and deploy it again.

## Settings

Add these to the `ai-engine` service under **Environment**. All are optional.

| Setting | Effect |
|---|---|
| `ADMIN_EMAIL`, `ADMIN_PASSWORD` | Create the admin account at start (set both) |
| `GOOGLE_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_ENDPOINT` | A model provider key from the environment. A key saved in the portal under **Settings › Model keys** wins |
| `EMBED_MODEL`, `EMBED_BASE_URL`, `EMBED_API_KEY`, `EMBED_PROVIDER` | An embedding model for knowledge bases |
| `PORTAL_NAME` | The name shown in the portal |

## Upgrade

`ai-engine` runs the `latest` image. To move to a newer release, open the
service and choose **Manual Deploy**. The engine applies new database
migrations when it starts.

To stay on one release, fork the repository, change the image in
`render.yaml` to a fixed tag such as `ghcr.io/waj-1/ai-engine:0.1.0`, and
deploy the Blueprint from your fork.

## Back up

- The database: Render takes snapshots of the disk. For a portable copy, open
  a shell on `ai-engine-db` and run `pg_dump -U postgres -h 127.0.0.1 -Fc postgres`.
- `SECRET_KEY`: copy its value from the `ai-engine` service's environment and
  keep it as carefully as a password. Model keys saved in the portal cannot
  be read without it.

## Good to know

- Keep the four private services private. The gateway on this platform has no
  API key check of its own, because only the engine can reach it. The REST
  layer and the auth service still verify every token.
- The database keeps its files in a `pgdata` folder on the disk, not at the
  top of it.
- All services must be in the same region to reach each other. `render.yaml`
  sets none, so they all get your workspace's default.
- Changing `POSTGRES_PASSWORD` in the dashboard after the first deploy does
  not change the password inside the database. Leave it as generated.

## What we could not check without a Render account

- That the image-backed web service becomes healthy within Render's deploy
  window on the first deploy. The engine waits up to 15 minutes
  (`INIT_WAIT_SEC=900`) for the other services.
- That a value made by `generateValue` reaches the other services through
  `fromService` with `envVarKey` exactly as written. The schema allows it.
- The plan sizes. They come from the memory the services used on our machine
  while idle, with room to spare.
