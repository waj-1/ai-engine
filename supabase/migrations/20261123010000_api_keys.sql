-- Portal-issued API keys (Settings > API keys; core/api_keys.py).
--
-- A key authenticates a caller of POST /v1/agents/{slug}/chat (scope `chat`
-- or `admin`) and of the admin MCP server at /mcp (scope `admin`) in place of
-- a signed-in operator's token. Only a SHA-256 digest of the key is stored:
-- the key itself is shown once, when it is created, and cannot be recovered.
-- `key_prefix` is the first characters of the key, kept so a person can tell
-- their keys apart in a list.
--
-- Rows are never deleted: revoking sets `revoked_at`, so the audit log and the
-- chat sessions a key created (ai_chat_sessions.user_id = api_keys.id) keep a
-- row to point at.
CREATE TABLE IF NOT EXISTS public.api_keys (
    id uuid DEFAULT gen_random_uuid() NOT NULL PRIMARY KEY,
    name text NOT NULL,
    key_prefix text NOT NULL,
    key_hash text NOT NULL,
    scopes text[] NOT NULL DEFAULT ARRAY['chat']::text[],
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    last_used_at timestamp with time zone,
    expires_at timestamp with time zone,
    revoked_at timestamp with time zone,
    revoked_by uuid,
    CONSTRAINT api_keys_name_check CHECK (char_length(name) BETWEEN 1 AND 120),
    CONSTRAINT api_keys_scopes_check CHECK (
        cardinality(scopes) >= 1 AND scopes <@ ARRAY['chat', 'admin']::text[]
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS api_keys_key_hash_key ON public.api_keys (key_hash);

COMMENT ON TABLE public.api_keys IS
    'Portal-issued API keys. Stores a SHA-256 digest and a display prefix, never the key.';

-- Service role only, like the other engine tables (the engine is the one
-- reader; no client token may read key digests).
ALTER TABLE public.api_keys ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "api_keys_service_role_all" ON public.api_keys;
CREATE POLICY "api_keys_service_role_all" ON public.api_keys
    AS PERMISSIVE FOR ALL TO service_role
    USING (true) WITH CHECK (true);

-- Belt and braces: client roles get no table privilege at all, so a policy
-- added by mistake later still exposes nothing.
REVOKE ALL ON TABLE public.api_keys FROM anon, authenticated;
GRANT ALL ON TABLE public.api_keys TO service_role;
