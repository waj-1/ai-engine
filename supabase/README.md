# AI engine Supabase project

The engine and its admin portal keep their data in their **own** Supabase
project. This tree owns that project's schema, migrations, RLS and triggers.

## What lives here
- `migrations/` — forward-only SQL, starting from the public baseline.
- `db-functions/{rls,triggers,validation,rpc}/` — idempotent `CREATE OR REPLACE`
  wrappers, deployed with `npm run deploy-ai-functions`.
- `config.toml` — local stack on ports **544xx**, so it can run next to another
  Supabase stack on the default 543xx ports.

## What stays in the auth project
Sign-in (`auth.users`, `public.user`, the superuser flag). `SUPABASE_*` points at
that project and `AI_SUPABASE_*` at this one; both can be the same project.

## Local development

The quickest path is `deploy/local/README.md`, which starts everything with one
compose file. To use the Supabase CLI instead:

```bash
# 1. Start this AI stack (544xx)
cd supabase && supabase start

# 2. Apply the baseline + forward migrations
supabase db reset
#    ^ migrations only — db-functions are NOT replayed by `db reset`.

# 3. Deploy db-functions to the local AI database
cd ..
AI_DATABASE_URL="postgresql://postgres:postgres@127.0.0.1:54422/postgres" npm run deploy-ai-functions

# 4. Point the engine at it in .env (AI_SUPABASE_*), and SUPABASE_* at the
#    project that signs users in.
```

## Env vars
See `.env.example` (`AI_SUPABASE_*`, `AI_DATABASE_URL`).

## Environments
One AI project can serve staging and production: rows carry an `environment`
column, and promotion copies a staging row to its production sibling. Apply
migrations before deploying code that needs them.
