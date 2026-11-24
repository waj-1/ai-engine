# Development

## Run the engine with Python

To run the engine with Python instead of in a container, see
[deploy/local/README.md](../deploy/local/README.md).

## Run the tests

The suite needs Python 3.11 and no running services, only placeholder settings
so the engine can import (`make develop` creates `venv/`):

```bash
ENVIRONMENT=development \
SUPABASE_URL=http://127.0.0.1:59998 SUPABASE_ANON_KEY=test-anon \
SUPABASE_SERVICE_KEY=test-service SUPABASE_JWT_SECRET=test-jwt-secret \
AI_SUPABASE_URL=http://127.0.0.1:59999 AI_SUPABASE_ANON_KEY=test-ai-anon \
AI_SUPABASE_SERVICE_KEY=test-ai-service \
GOOGLE_API_KEY=test-google OPENAI_API_KEY=test-openai \
ALLOWED_ORIGINS="*" \
./venv/bin/python -m pytest tests/ -q
```

Run it in a shell where `deploy/local/.env` is not loaded:
`ENABLE_GENERIC_AGENTS`, `ENABLE_ADMIN_MCP` and `AI_DATABASE_URL` must be unset.

## Contributing

See [CONTRIBUTING.md](../CONTRIBUTING.md) for how to propose a change.
