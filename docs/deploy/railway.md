# Run WAJ AI Engine on Railway

> **Status: validated against Railway's documentation and SDK, not yet
> deployed by us.** `deploy/railway/railway.ts` type-checks against Railway's
> `railway` package (3.17.0) and evaluates to the five services and one volume
> described below. We ran the same five services, wired the same way, with
> Docker Compose on a Linux machine: the stack came up healthy and the admin
> sign-in worked. We have not applied the file to a Railway project, and there
> is no published template yet. If a step below is wrong, please open an issue.

## What the repository can and cannot do

Railway has two ways to describe a deployment, and neither is a file that a
"Deploy on Railway" button can read from a repository:

| | What it is | Where it lives |
|---|---|---|
| Infrastructure as Code | A TypeScript file that describes a whole project. The Railway CLI applies it to **your own** project | In this repository: [`deploy/railway/railway.ts`](../../deploy/railway/railway.ts) |
| A template | What the "Deploy on Railway" button opens. It is composed and published in Railway's dashboard and gets a template code | In Railway, under the publisher's account. **Not published yet** |

So today you can deploy with the CLI (next section). The one-click button
needs the project owner to publish a template first (last section).

There is no `railway.json` or `railway.toml` in this repository on purpose.
Railway has deprecated those files: new services cannot use them, and
existing ones stop being read on 2026-12-01.

## What gets created

| Service | Source | Notes |
|---|---|---|
| `Engine` | Image `ghcr.io/waj-1/ai-engine:latest` | The engine and its admin portal. The only service to give a public domain |
| `Gateway` | This repository, `deploy/paas/gateway.Dockerfile` | One address in front of the next two. Private |
| `REST` | Image `postgrest/postgrest` | The REST layer. Private |
| `Auth` | Image `ghcr.io/supabase/gotrue` | The auth service. Private |
| `Database` | This repository, `deploy/paas/db.Dockerfile` | Postgres with pgvector, with a volume at `/var/lib/postgresql/data`. Private |

The services find each other on the project's private network
(`<service>.railway.internal`).

Secrets are environment variables, each stored on the service that owns it
and read by the others by reference:

| Secret | Stored on | Used for |
|---|---|---|
| `POSTGRES_PASSWORD` | `Database` | The database password |
| `GOTRUE_JWT_SECRET` | `Auth` | Signing every access token |
| `SECRET_KEY` | `Engine` | Encrypting model keys saved in the portal |

This is different from `docker compose up`, where a setup step writes the
secrets into a shared Docker volume. Railway services cannot share a volume,
so [`deploy/paas/start_engine.py`](../../deploy/paas/start_engine.py) turns
the variables into the engine's settings and signs the engine's two API
tokens from the signing secret at each start.

## Deploy to your own project with the CLI

You need a Railway account, the [Railway CLI](https://docs.railway.com/cli)
(5.42.1 or newer), Node.js, and an **empty** Railway project. Use a new
project: applying the file to a project that has other services plans to
delete them.

```bash
git clone https://github.com/waj-1/ai-engine.git
cd ai-engine
npm install railway            # Railway's SDK, which the CLI uses to read the file

railway login
railway init                   # or `railway link` to an empty project

# The three secrets, made on your machine and sent to Railway once.
export ENGINE_POSTGRES_PASSWORD="$(openssl rand -hex 24)"
export ENGINE_JWT_SECRET="$(openssl rand -hex 32)"
export ENGINE_SECRET_KEY="$(openssl rand -hex 32)"

railway config plan  --file deploy/railway/railway.ts    # shows what it would create
railway config apply --file deploy/railway/railway.ts
```

Then, in the Railway dashboard:

1. Open the `Engine` service, **Settings**, **Networking**, and choose
   **Generate Domain**. Target port: `8000`.
2. Wait for `Engine` to turn healthy. The first deploy takes several minutes:
   the engine waits for `Database` and `Gateway` to be built, then applies
   the database schema. `Auth` restarts a few times until the database is up.
   That is expected.

Later applies need no `ENGINE_*` variables. An unset one means "keep the
value Railway already has". Keep `ENGINE_SECRET_KEY` somewhere safe all the
same: model keys saved in the portal cannot be read without it.

## First login

There is no default login. Open the `Engine` domain. The first-run page asks
for a one-time setup code and creates the admin account. The code is in the
`Engine` service's deploy logs (look for `Setup code`).

To skip the browser step, add both `ADMIN_EMAIL` and `ADMIN_PASSWORD` to the
`Engine` service's variables and redeploy it.

## Settings

Add these to the `Engine` service's variables. All are optional.

| Setting | Effect |
|---|---|
| `ADMIN_EMAIL`, `ADMIN_PASSWORD` | Create the admin account at start (set both) |
| `GOOGLE_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_ENDPOINT` | A model provider key from the environment. A key saved in the portal under **Settings › Model keys** wins |
| `EMBED_MODEL`, `EMBED_BASE_URL`, `EMBED_API_KEY`, `EMBED_PROVIDER` | An embedding model for knowledge bases |
| `PORTAL_NAME` | The name shown in the portal |

## Good to know

- **Private networking over IPv4.** The services listen on IPv4. Railway
  environments created on or after 2025-10-16 support IPv4 on the private
  network. Older environments are IPv6 only and will not work: create a new
  environment or project.
- Do not give `Gateway`, `REST`, `Auth` or `Database` a public domain or TCP
  proxy. The gateway on this platform has no API key check of its own,
  because only the engine can reach it.
- The database keeps its files in a `pgdata` folder on the volume.
- `Engine` runs the `latest` image. Redeploy the service to pull a newer
  release. To stay on one release, change `ENGINE_IMAGE` at the top of
  `railway.ts` to a fixed tag and apply again.

## For the project owner: publish the one-click template

This cannot be done from the repository. It needs a Railway account that
will be shown as the template's author.

1. Deploy with the CLI as above into a new project, generate the `Engine`
   domain, and check that the first-run page opens and you can sign in. This
   is also the first real test of `railway.ts`.
2. In that project open **Settings** and, under **Generate Template from
   Project**, choose **Create Template**. The template composer opens with
   the five services.
3. In the composer, replace the three secret values with template functions,
   so every deploy gets its own:

   | Service | Variable | Value |
   |---|---|---|
   | `Database` | `POSTGRES_PASSWORD` | `${{secret(32)}}` |
   | `Auth` | `GOTRUE_JWT_SECRET` | `${{secret(48)}}` |
   | `Engine` | `SECRET_KEY` | `${{secret(48)}}` |

   Check that no real secret from your test project is left in any variable.
4. Check the rest against the tables at the end of this page: sources, start
   command, healthcheck, the volume on `Database`, and that only `Engine` has
   public networking (HTTP, port 8000).
5. Add an icon and the overview text, then **Create Template**.
6. Deploy the template once into a fresh project to test it.
7. On the workspace's **Templates** page choose **Publish** and fill in the
   form. Copy the template code from the template's URL
   (`https://railway.com/new/template/<code>`).
8. Add the button to the README with that code:

   ```md
   [![Deploy on Railway](https://railway.com/button.svg)](https://railway.com/new/template/<code>?utm_medium=integration&utm_source=button&utm_campaign=waj-ai-engine)
   ```

If "Generate Template from Project" is not available, build the template by
hand: workspace settings, **Templates**, **New Template**, and add the five
services from the tables below.

### The five services, for the template composer

Every value in `${{…}}` is a Railway reference to another service's variable.

**Database**. Source: GitHub repo `https://github.com/waj-1/ai-engine`.
Variables: `RAILWAY_DOCKERFILE_PATH=deploy/paas/db.Dockerfile`,
`POSTGRES_PASSWORD=${{secret(32)}}`. Volume: mount path
`/var/lib/postgresql/data`. No public networking.

**Auth**. Source: Docker image `ghcr.io/supabase/gotrue:v2.158.1`. No public
networking. Variables:

| Variable | Value |
|---|---|
| `GOTRUE_JWT_SECRET` | `${{secret(48)}}` |
| `PGHOST` | `${{Database.RAILWAY_PRIVATE_DOMAIN}}` |
| `PGPORT` | `5432` |
| `PGDATABASE` | `postgres` |
| `PGUSER` | `postgres` |
| `PGPASSWORD` | `${{Database.POSTGRES_PASSWORD}}` |
| `GOTRUE_DB_DRIVER` | `postgres` |
| `GOTRUE_DB_DATABASE_URL` | `postgres:///postgres?search_path=auth` |
| `GOTRUE_API_HOST` | `0.0.0.0` |
| `GOTRUE_API_PORT` | `9999` |
| `API_EXTERNAL_URL` | `http://gateway.railway.internal:8000` |
| `GOTRUE_SITE_URL` | `http://engine.railway.internal` |
| `GOTRUE_URI_ALLOW_LIST` | `*` |
| `GOTRUE_DISABLE_SIGNUP` | `true` |
| `GOTRUE_JWT_ADMIN_ROLES` | `service_role` |
| `GOTRUE_JWT_AUD` | `authenticated` |
| `GOTRUE_JWT_DEFAULT_GROUP_NAME` | `authenticated` |
| `GOTRUE_JWT_EXP` | `3600` |
| `GOTRUE_EXTERNAL_EMAIL_ENABLED` | `true` |
| `GOTRUE_MAILER_AUTOCONFIRM` | `true` |

**REST**. Source: Docker image `postgrest/postgrest:v12.2.3`. No public
networking. Variables:

| Variable | Value |
|---|---|
| `PGHOST` | `${{Database.RAILWAY_PRIVATE_DOMAIN}}` |
| `PGPORT` | `5432` |
| `PGDATABASE` | `postgres` |
| `PGUSER` | `authenticator` |
| `PGPASSWORD` | `${{Database.POSTGRES_PASSWORD}}` |
| `PGRST_DB_URI` | `postgresql://` |
| `PGRST_DB_SCHEMAS` | `public` |
| `PGRST_DB_ANON_ROLE` | `anon` |
| `PGRST_DB_USE_LEGACY_GUCS` | `false` |
| `PGRST_SERVER_HOST` | `0.0.0.0` |
| `PGRST_SERVER_PORT` | `3000` |
| `PGRST_JWT_SECRET` | `${{Auth.GOTRUE_JWT_SECRET}}` |

**Gateway**. Source: GitHub repo `https://github.com/waj-1/ai-engine`. No
public networking. Variables:
`RAILWAY_DOCKERFILE_PATH=deploy/paas/gateway.Dockerfile`,
`REST_HOST=${{REST.RAILWAY_PRIVATE_DOMAIN}}`,
`AUTH_HOST=${{Auth.RAILWAY_PRIVATE_DOMAIN}}`.

**Engine**. Source: Docker image `ghcr.io/waj-1/ai-engine:latest`. Start
command: `python deploy/paas/start_engine.py`. Healthcheck path: `/health`.
Public networking: HTTP, port 8000. Variables:

| Variable | Value |
|---|---|
| `PORT` | `8000` |
| `SECRET_KEY` | `${{secret(48)}}` |
| `JWT_SECRET` | `${{Auth.GOTRUE_JWT_SECRET}}` |
| `POSTGRES_PASSWORD` | `${{Database.POSTGRES_PASSWORD}}` |
| `GATEWAY_HOST` | `${{Gateway.RAILWAY_PRIVATE_DOMAIN}}` |
| `GATEWAY_PORT` | `8000` |
| `DB_HOST` | `${{Database.RAILWAY_PRIVATE_DOMAIN}}` |
| `DB_PORT` | `5432` |
| `INIT_WAIT_SEC` | `540` |

Set every service's restart policy to **Always**.

## What we could not check without a Railway account

- `railway config plan` and `apply` against a real project.
- That a GitHub-sourced service builds from the Dockerfile path given in
  `railway.ts` with the repository root as the build context. Both
  Dockerfiles need the root as context.
- Volume ownership: the database's entrypoint must be able to create its
  `pgdata` folder on the volume. If it cannot, set `RAILWAY_RUN_UID=0` on
  `Database`.
- The template composer steps, which are written from Railway's
  documentation as of November 2026.
