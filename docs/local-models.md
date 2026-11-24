# Local models: run with no API key

A chat model and an embedding model served on your own machine work with no
key at all. This page covers both, for the `docker compose up` stack. For the
hand-run Python stack see [deploy/local/README.md](../deploy/local/README.md).

## A chat model with Ollama

```bash
ollama pull qwen2.5:3b
```

Add it under **Agents › Models › New model**: provider **Ollama**, model
string `qwen2.5:3b`.

### Let the container reach Ollama

The engine's container reaches Ollama on your machine at
`host.docker.internal:11434`. Ollama has to listen on an address containers
can reach, not only on `127.0.0.1`. Either:

- start Ollama with `OLLAMA_HOST=0.0.0.0`, or
- on Linux with Ollama in Docker, publish it on the Docker bridge address:

  ```bash
  docker run -d --name ollama -p 172.17.0.1:11434:11434 -v ollama:/root/.ollama ollama/ollama
  docker exec ollama ollama pull qwen2.5:3b
  ```

If the portal cannot reach a local model, this is the usual cause.

## Any other OpenAI-compatible server

For vLLM, LM Studio or a hosted catalog, choose provider **OpenAI** and fill
in **Base URL**.

If a server on your machine or network asks for a key of its own, put the key
in an environment variable and name that variable in the model's
`extra_config` as `api_key_env`. Without it the engine sends a placeholder key
and the server answers 401.

## Knowledge bases with no API key

Documents are made searchable by an embedding model. Without a Gemini key, use
one on your machine:

```bash
ollama pull nomic-embed-text
echo "EMBED_MODEL=nomic-embed-text" >> .env
docker compose up -d
```

Any OpenAI-compatible server works (`EMBED_BASE_URL`, `EMBED_MODEL`). The
**Knowledge** screen names the model in use, or says what to set when there is
none.

Choose the model before uploading. Changing it later means re-embedding every
document:

```bash
python -m scripts.reembed --target kb --apply
```

The screen marks a knowledge base that needs it.

Attach a knowledge base on an agent's **Tools & knowledge** tab and the agent
gets a `search_knowledge` tool. See
[Add documents to a knowledge base](partner/03-knowledge/01-knowledge-bases.md).

## What to expect from a small model

A small model follows short instructions and calls tools most of the time. Use
a larger one for anything real.

A small chat model does not always call `search_knowledge`. In our run a 1.5B
model answered without searching, and a 3B model searched and answered from
the document.
