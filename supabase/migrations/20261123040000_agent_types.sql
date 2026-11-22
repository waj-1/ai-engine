-- User-defined agent categories ("Support", "Sales", "Internal tools").
--
-- A category is DATA: a row here, never a new ai_agent_kind enum value and
-- never code. It groups, filters and reports on agents and has no effect on
-- how an agent runs: `agent_kind` still names the code that runs a profile,
-- and a generic agent is still resolved by (custom, slug, environment).
--
-- The category is not part of a promote snapshot (agent_profile_versions):
-- renaming or recolouring one, or moving an agent between categories, shows
-- at once and needs no re-promote.
--
-- A fresh database has no categories. Service-role-only table, registered in
-- db-functions/rls/agent_tables.ts.

CREATE TABLE IF NOT EXISTS public.agent_types (
    agent_type_id uuid DEFAULT gen_random_uuid() NOT NULL,
    slug text NOT NULL,
    display_name text NOT NULL,
    description text,
    color text DEFAULT 'slate'::text NOT NULL,
    sort_order integer DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    CONSTRAINT agent_types_pkey PRIMARY KEY (agent_type_id),
    CONSTRAINT agent_types_slug_key UNIQUE (slug),
    CONSTRAINT agent_types_color_check CHECK (color = ANY (ARRAY[
        'slate'::text, 'teal'::text, 'blue'::text, 'violet'::text,
        'pink'::text, 'red'::text, 'amber'::text, 'green'::text]))
);

COMMENT ON TABLE public.agent_types IS
    'User-defined agent categories. Organisational only: a category never selects code and is not frozen into agent_profile_versions snapshots.';
COMMENT ON COLUMN public.agent_types.slug IS
    'Stable lower-case identifier (^[a-z][a-z0-9-]{0,47}$, enforced at the admin layer). The handle the admin MCP tools use; it does not change when the category is renamed.';
COMMENT ON COLUMN public.agent_types.color IS
    'One of a fixed palette of colour keys; the portal maps each to a chip style.';
COMMENT ON COLUMN public.agent_types.sort_order IS
    'Position in pickers, filters and the categories screen (ascending).';

ALTER TABLE public.agent_profiles
    ADD COLUMN IF NOT EXISTS agent_type_id uuid;

COMMENT ON COLUMN public.agent_profiles.agent_type_id IS
    'Optional user-defined category (agent_types). NULL = uncategorised. Deleting a category sets this to NULL.';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'agent_profiles_agent_type_id_fkey'
          AND conrelid = 'public.agent_profiles'::regclass
    ) THEN
        ALTER TABLE public.agent_profiles
            ADD CONSTRAINT agent_profiles_agent_type_id_fkey
            FOREIGN KEY (agent_type_id)
            REFERENCES public.agent_types(agent_type_id) ON DELETE SET NULL;
    END IF;
END
$$;

CREATE INDEX IF NOT EXISTS idx_agent_profiles_agent_type
    ON public.agent_profiles (agent_type_id) WHERE agent_type_id IS NOT NULL;

ALTER TABLE public.agent_types ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "agent_types_service_role_all" ON public.agent_types;
CREATE POLICY "agent_types_service_role_all" ON public.agent_types
    AS PERMISSIVE FOR ALL TO service_role
    USING (true) WITH CHECK (true);
