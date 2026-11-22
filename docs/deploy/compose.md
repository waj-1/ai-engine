# Run WAJ AI Engine with Docker Compose on one machine

This is the one deployment we have run ourselves, on a Linux machine with
Docker Engine and the Compose plugin. Every command below was run on a fresh
install before it was written down. We have not yet tested any other target
(a cloud container service, Kubernetes, a public address behind a TLS proxy),
so there is no guide for those yet.

## What you get

`docker compose up` builds the engine image and starts seven services: a setup
step, Postgres with pgvector, a REST layer, an auth service, a gateway, an init
step and the engine. Two Docker volumes hold everything worth keeping:

| Volume | Holds |
|---|---|
| `waj-ai-engine_pg-data` | The database: agents, instructions, knowledge, conversations, API keys, encrypted model keys |
| `waj-ai-engine_state` | This install's secrets: the database password, the signing secret, `SECRET_KEY` |

Two ports are published, on this machine only (`127.0.0.1`): the engine on
8000 and the gateway on 54421. The database is not published at all.

## Start

```bash
git clone https://github.com/waj-1/ai-engine.git
cd ai-engine
docker compose up -d
```

The first start builds the image, which takes a few minutes. Then either:

- **In a browser.** Open http://localhost:8000. The first-run page asks for
  the setup code from `docker compose logs engine`, creates the admin account,
  offers to store a model key, and shows your first API key once.
- **With no browser step.** Set both variables before the first start, and the
  account is created at start instead:

  ```bash
  ADMIN_EMAIL=you@example.com ADMIN_PASSWORD='a long passphrase' docker compose up -d
  ```

  Setting only one of the two stops the stack with a message saying so.

Check it is healthy:

```bash
docker compose ps
curl http://localhost:8000/health
```

## Settings

Put settings in a `.env` file next to `docker-compose.yml`; Compose reads it
on `up`. All are optional.

| Setting | Effect |
|---|---|
| `ADMIN_EMAIL`, `ADMIN_PASSWORD` | Create the admin account at the first start (set both) |
| `GOOGLE_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `AZURE_OPENAI_API_KEY` | A model provider key from the environment. A key saved under **Settings › Model keys** wins |
| `EMBED_MODEL`, `EMBED_BASE_URL` | A local embedding model for knowledge bases, for example `EMBED_MODEL=nomic-embed-text` with Ollama |
| `PORTAL_NAME` | The name shown in the portal |
| `ENGINE_PORT`, `GATEWAY_PORT` | Publish on other ports than 8000 and 54421 |
| `SEED_DEMO_AGENT=false` | Do not create the demo agent |

After changing `.env`, run `docker compose up -d` again; only the engine
restarts.

### A model on the same machine

The engine's container reaches Ollama on the host at
`host.docker.internal:11434`. Ollama has to listen on an address containers can
reach, not only on `127.0.0.1`: start it with `OLLAMA_HOST=0.0.0.0`, or run it
in Docker published on the Docker bridge address:

```bash
docker run -d --name ollama -p 172.17.0.1:11434:11434 -v ollama:/root/.ollama ollama/ollama
docker exec ollama ollama pull qwen2.5:3b
```

Then add it in the portal under **Agents › Models › New model**, provider
**Ollama**.

## Stop, restart, upgrade

```bash
docker compose stop          # stop, keep everything
docker compose up -d         # start again: same secrets, same login, same data
```

To upgrade to a newer version of the code:

```bash
git pull
docker compose up -d --build
```

The image is rebuilt, the init step applies any new database migration (it
runs on every start and is safe to repeat), and the engine restarts. The
volumes are untouched.

## Back up

Back up both volumes. The database backup is useless without the state
volume: model keys saved in the portal are encrypted with the `SECRET_KEY`
kept there.

```bash
docker compose exec -T db pg_dump -U postgres -Fc postgres > engine-db.dump
docker run --rm -v waj-ai-engine_state:/s:ro -v "$PWD":/b alpine \
  tar czf /b/engine-state.tgz -C /s .
```

Keep `engine-state.tgz` as carefully as a password file.

## Restore

On a machine with the repository cloned and no existing install (or after
`docker compose down -v`):

```bash
# 1. The secrets first, so the stack starts as the same install
docker volume create waj-ai-engine_state
docker run --rm -v waj-ai-engine_state:/s -v "$PWD":/b:ro alpine \
  tar xzf /b/engine-state.tgz -C /s

# 2. The database alone, and wait until it reports healthy
docker compose up -d db
docker compose ps db

# 3. The data
docker compose exec -T db pg_restore -U postgres -d postgres \
  --clean --if-exists --no-owner < engine-db.dump

# 4. Everything else
docker compose up -d
```

The admin login, API keys, agents and conversations are the ones from the
backup, and the first-run page stays closed. Compose prints a warning that the
state volume "already exists but was not created by Docker Compose"; that is
expected here.

## Start over

```bash
docker compose down -v
```

This deletes the database and the secrets. The next `docker compose up` is a
new install with a new setup code.

## When something is wrong

| Symptom | Look at |
|---|---|
| `up` stops at `setup` | `docker compose logs setup`: usually only one of `ADMIN_EMAIL` / `ADMIN_PASSWORD` was set |
| `up` stops at `init` | `docker compose logs init`: the database or the auth service did not come up, or the admin account could not be created |
| The first-run page says setup is locked | Five wrong codes were entered. `docker compose restart engine` prints a new code |
| An agent says no model is configured | Add a model under **Agents › Models**, or a key under **Settings › Model keys** |
| The portal cannot reach a local model | Ollama is listening on `127.0.0.1` only; see "A model on the same machine" |

## What this guide does not cover

Serving the engine to other machines. Both ports are bound to `127.0.0.1` on
purpose: the engine speaks plain HTTP and expects a TLS-terminating proxy in
front of it before it is exposed. We have not tested that setup yet, so it is
not documented here.
