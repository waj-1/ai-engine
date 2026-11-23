-- Model provider keys entered in the portal (Settings > Model keys), stored
-- encrypted. `ciphertext` is a Fernet token made with a key derived from the
-- server's SECRET_KEY (core/provider_keys.py); the database never holds a key
-- in plain text, and the engine never stores one when SECRET_KEY is unset.
-- `name` is '' for a provider's default key; a non-empty name is a second,
-- named credential a model selects with extra_config.credential.
CREATE TABLE IF NOT EXISTS public.provider_keys (
    key_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    environment public.agent_environment NOT NULL,
    provider text NOT NULL,
    name text NOT NULL DEFAULT '',
    ciphertext text NOT NULL,
    hint text NOT NULL DEFAULT '',
    created_at timestamp with time zone NOT NULL DEFAULT now(),
    created_by uuid,
    updated_at timestamp with time zone NOT NULL DEFAULT now(),
    updated_by uuid,
    CONSTRAINT provider_keys_environment_provider_name_key UNIQUE (environment, provider, name)
);

COMMENT ON TABLE public.provider_keys IS 'Model provider keys stored encrypted (Fernet, key derived from SECRET_KEY). Written by admin/services/provider_keys.py; read by core/provider_keys.py, which prefers a stored key over the environment variable.';
COMMENT ON COLUMN public.provider_keys.ciphertext IS 'Encrypted key. Never returned by the portal or the admin MCP.';
COMMENT ON COLUMN public.provider_keys.hint IS 'Last four characters of the key, for telling keys apart.';

ALTER TABLE public.provider_keys ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "provider_keys_service_role_all" ON public.provider_keys;
CREATE POLICY "provider_keys_service_role_all" ON public.provider_keys
    AS PERMISSIVE FOR ALL TO service_role
    USING (true) WITH CHECK (true);

-- Only the engine's service role reads or writes this table.
REVOKE ALL ON TABLE public.provider_keys FROM anon, authenticated;
GRANT ALL ON TABLE public.provider_keys TO service_role;
