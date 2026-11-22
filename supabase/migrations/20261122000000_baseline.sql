-- WAJ AI Engine: database baseline.
--
-- One migration that creates the whole schema the engine uses on an empty
-- Supabase Postgres. Later migrations go next to it, in timestamp order.

-- Functions are created before the tables they read, as pg_dump orders them.
SET check_function_bodies = false;

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS vector;

-- Built from the private schema as of 20261122120000_add_custom_agent_kind.sql.

--
-- Name: SCHEMA public; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON SCHEMA public IS 'standard public schema';

--
-- Name: agent_data_source_kind; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.agent_data_source_kind AS ENUM (
    'pgvector',
    'ms_drive',
    'gitbook',
    'supabase_table',
    'http'
);

--
-- Name: agent_environment; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.agent_environment AS ENUM (
    'dev',
    'staging',
    'prod'
);

--
-- Name: agent_eval_run_status; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.agent_eval_run_status AS ENUM (
    'queued',
    'running',
    'passed',
    'failed',
    'errored',
    'cancelled'
);

--
-- Name: agent_mcp_transport; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.agent_mcp_transport AS ENUM (
    'stdio',
    'sse',
    'http'
);

--
-- Name: agent_tool_scope; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.agent_tool_scope AS ENUM (
    'read',
    'write'
);

--
-- Name: ai_agent_kind; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.ai_agent_kind AS ENUM (
    'custom',
    'agents_support',
    'agents_inquiries',
    'agents_collection',
    'agents_sales'
);

--
-- Name: TYPE ai_agent_kind; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TYPE public.ai_agent_kind IS 'Which AI agent owns an ai_chat_sessions row. Add new agents via ALTER TYPE ... ADD VALUE.';

--
-- Name: ai_conversation_insight_candidates(integer, integer, integer, boolean); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.ai_conversation_insight_candidates(p_idle_minutes integer DEFAULT 30, p_lookback_days integer DEFAULT 30, p_limit integer DEFAULT 40, p_retry_unclassified boolean DEFAULT false) RETURNS TABLE(session_id uuid, agent_kind text, metadata jsonb, created_at timestamp with time zone, updated_at timestamp with time zone)
    LANGUAGE sql STABLE
    AS $$
    SELECT s.id, s.agent_kind::text, s.metadata, s.created_at, s.updated_at
    FROM public.ai_chat_sessions s
    LEFT JOIN public.ai_conversation_insights i ON i.session_id = s.id
    WHERE s.deleted_at IS NULL
      AND s.updated_at < now() - make_interval(mins => p_idle_minutes)
      AND s.updated_at > now() - make_interval(days => p_lookback_days)
      AND (i.session_id IS NULL
           OR i.last_message_at < s.updated_at - interval '1 second'
           OR (p_retry_unclassified AND i.classifier_model IS NULL
               AND NOT i.is_simulated
               AND i.analyzed_at < now() - interval '6 hours'))
      AND EXISTS (
          SELECT 1 FROM public.ai_chat_messages m
          WHERE m.session_id = s.id AND m.role = 'user'
      )
    ORDER BY s.updated_at DESC
    LIMIT GREATEST(1, LEAST(p_limit, 500));
$$;

--
-- Name: ai_conversation_insights_rollup(integer, text, text[]); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.ai_conversation_insights_rollup(p_hours integer, p_env text DEFAULT NULL::text, p_kinds text[] DEFAULT NULL::text[]) RETURNS TABLE(period text, agent_kind text, outcome text, topic text, language text, channel text, n bigint)
    LANGUAGE sql STABLE
    AS $$
    SELECT CASE WHEN i.last_message_at > now() - make_interval(hours => p_hours)
                THEN 'cur' ELSE 'prev' END,
           i.agent_kind, i.outcome, i.topic, i.language, i.channel, count(*)
    FROM public.ai_conversation_insights i
    WHERE i.last_message_at > now() - make_interval(hours => 2 * p_hours)
      AND NOT i.is_simulated
      AND (p_env IS NULL
           OR i.environment = p_env
           OR (p_env = 'staging' AND i.environment IS NULL))
      AND (p_kinds IS NULL OR i.agent_kind = ANY (p_kinds))
    GROUP BY 1, 2, 3, 4, 5, 6;
$$;

--
-- Name: ai_reserve_voice_seconds(uuid, integer, integer); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.ai_reserve_voice_seconds(p_business_id uuid, p_seconds integer, p_limit integer) RETURNS integer
    LANGUAGE plpgsql
    AS $$
DECLARE
  v_today date := (now() AT TIME ZONE 'utc')::date;
  v_current integer;
  v_total integer;
BEGIN
  -- Ensure the row exists, then lock it: two concurrent reservations
  -- serialize on the row lock instead of both reading the same "current".
  INSERT INTO public.ai_voice_usage_daily (business_id, usage_date, seconds_used)
  VALUES (p_business_id, v_today, 0)
  ON CONFLICT (business_id, usage_date) DO NOTHING;

  SELECT seconds_used INTO v_current
    FROM public.ai_voice_usage_daily
   WHERE business_id = p_business_id AND usage_date = v_today
   FOR UPDATE;

  v_total := v_current + GREATEST(p_seconds, 0);

  -- Clamp the RECORDED tally at the limit (never below what's already
  -- recorded — a lowered limit must not erase spend that already happened).
  UPDATE public.ai_voice_usage_daily
     SET seconds_used = GREATEST(v_current, LEAST(v_total, GREATEST(p_limit, 0))),
         updated_at = now()
   WHERE business_id = p_business_id AND usage_date = v_today;

  -- PRE-clamp total: the caller compares this to the limit to decide
  -- exhaustion, exactly like the legacy Python path did.
  RETURN v_total;
END;
$$;

--
-- Name: FUNCTION ai_reserve_voice_seconds(p_business_id uuid, p_seconds integer, p_limit integer); Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON FUNCTION public.ai_reserve_voice_seconds(p_business_id uuid, p_seconds integer, p_limit integer) IS 'Atomically add p_seconds to today''s duplex-voice tally for p_business_id, clamping the recorded value at p_limit. Returns the pre-clamp total (caller raises BudgetExhausted when it exceeds p_limit). Called by voice/live/budget.py::reserve_audio_seconds; the Python read-modify-write remains only as a fallback for DBs that predate this migration.';


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: admin_jobs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.admin_jobs (
    job_id uuid DEFAULT gen_random_uuid() NOT NULL,
    kind text NOT NULL,
    label text NOT NULL,
    related_entity_type text,
    related_entity_id text,
    status text DEFAULT 'queued'::text NOT NULL,
    started_at timestamp with time zone,
    finished_at timestamp with time zone,
    duration_ms integer,
    error text,
    triggered_by uuid,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    CONSTRAINT admin_jobs_kind_check CHECK ((kind = ANY (ARRAY['eval_run'::text, 'kb_upload'::text, 'deploy_dispatch'::text, 'env_promote'::text, 'tool_dry_run'::text, 'other'::text]))),
    CONSTRAINT admin_jobs_status_check CHECK ((status = ANY (ARRAY['queued'::text, 'running'::text, 'succeeded'::text, 'failed'::text, 'cancelled'::text])))
);

--
-- Name: TABLE admin_jobs; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.admin_jobs IS 'Lightweight tracker for fire-and-forget admin work (eval runs, KB ingestion, deploy dispatches). Source of truth for /admin/jobs UI. NOT a real queue — we still use daemon threads. The row gives us visibility + cancellation + crash post-mortems.';

--
-- Name: agent_audit_log; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_audit_log (
    audit_id uuid DEFAULT gen_random_uuid() NOT NULL,
    entity_type text NOT NULL,
    entity_id uuid,
    action text NOT NULL,
    before jsonb,
    after jsonb,
    actor_user_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid
);

--
-- Name: TABLE agent_audit_log; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.agent_audit_log IS 'Append-only change log for every config-plane mutation. Two-year retention enforced via reaper job.';

--
-- Name: agent_bakeoffs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_bakeoffs (
    bakeoff_id uuid DEFAULT gen_random_uuid() NOT NULL,
    name text NOT NULL,
    profile_id uuid NOT NULL,
    variants jsonb DEFAULT '[]'::jsonb NOT NULL,
    suite_ids uuid[] DEFAULT '{}'::uuid[] NOT NULL,
    status text DEFAULT 'running'::text NOT NULL,
    repeats integer DEFAULT 1 NOT NULL,
    notes text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    CONSTRAINT agent_bakeoffs_status_check CHECK ((status = ANY (ARRAY['running'::text, 'complete'::text, 'errored'::text])))
);

--
-- Name: TABLE agent_bakeoffs; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.agent_bakeoffs IS 'A named bake-off: N profile-version variants x M suites of one profile, compared across all metrics. Spawned runs link back via agent_eval_runs.bakeoff_id.';

--
-- Name: agent_channels; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_channels (
    channel_id uuid DEFAULT gen_random_uuid() NOT NULL,
    slug text NOT NULL,
    name text NOT NULL,
    kind text NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    allowed_origins text[] DEFAULT ARRAY[]::text[] NOT NULL,
    allowed_agent_kinds text[] DEFAULT ARRAY[]::text[] NOT NULL,
    default_profile_id uuid,
    business_id uuid,
    auth_required boolean DEFAULT true NOT NULL,
    rate_limit_rpm integer DEFAULT 60 NOT NULL,
    config jsonb DEFAULT '{}'::jsonb NOT NULL,
    description text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by uuid,
    updated_by uuid,
    CONSTRAINT agent_channels_kind_check CHECK ((kind = ANY (ARRAY['public_api'::text, 'embed'::text, 'other'::text])))
);

--
-- Name: COLUMN agent_channels.slug; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.agent_channels.slug IS 'Stable lower-snake identifier, used as the lookup key from the runtime. Format: ^[a-z][a-z0-9_-]{1,62}$ (enforced at the admin layer).';

--
-- Name: COLUMN agent_channels.allowed_origins; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.agent_channels.allowed_origins IS 'Exact-match CORS origins (scheme + host[:port], no path). An empty array means "no browser origin is allowed" — appropriate for server-to-server channels like public_api.';

--
-- Name: COLUMN agent_channels.allowed_agent_kinds; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.agent_channels.allowed_agent_kinds IS 'Whitelist of agent_kinds this channel may dispatch to. An empty array means "no restriction beyond the global allow-list".';

--
-- Name: COLUMN agent_channels.rate_limit_rpm; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.agent_channels.rate_limit_rpm IS 'Per-channel requests-per-minute cap. Admin form caps at 3600 (60 RPS).';

--
-- Name: agent_data_sources; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_data_sources (
    source_id uuid DEFAULT gen_random_uuid() NOT NULL,
    name text NOT NULL,
    kind public.agent_data_source_kind NOT NULL,
    config jsonb DEFAULT '{}'::jsonb NOT NULL,
    refresh_cadence text,
    freshness_sla_hours integer,
    last_refreshed_at timestamp with time zone,
    is_active boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    agent_kind public.ai_agent_kind,
    additional_agent_kinds text[] DEFAULT ARRAY[]::text[] NOT NULL
);

--
-- Name: COLUMN agent_data_sources.agent_kind; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.agent_data_sources.agent_kind IS 'Owning agent. Set by seed_data_sources at boot. Null only for legacy rows pre-2026-05-19.';

--
-- Name: COLUMN agent_data_sources.additional_agent_kinds; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.agent_data_sources.additional_agent_kinds IS 'Extra agent_kinds this source serves beyond the primary `agent_kind`. seed_agent_profiles unions both when building data_source_ids.';

--
-- Name: agent_deployment_targets; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_deployment_targets (
    target_id uuid DEFAULT gen_random_uuid() NOT NULL,
    environment public.agent_environment NOT NULL,
    label text NOT NULL,
    base_url text NOT NULL,
    auth_env_var text,
    description text,
    is_default boolean DEFAULT false NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid
);

--
-- Name: TABLE agent_deployment_targets; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.agent_deployment_targets IS 'Per-environment agent-service URLs. E2E runner + cross-env promote read here instead of AI_AGENT_BASE_URL_* env vars.';

--
-- Name: agent_e2e_runs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_e2e_runs (
    run_id uuid DEFAULT gen_random_uuid() NOT NULL,
    profile_id uuid NOT NULL,
    environment public.agent_environment NOT NULL,
    suite_id uuid,
    status text NOT NULL,
    score numeric,
    pass_count integer,
    fail_count integer,
    error text,
    results jsonb,
    triggered_by uuid,
    started_at timestamp with time zone,
    finished_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by uuid,
    updated_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT agent_e2e_runs_status_check CHECK ((status = ANY (ARRAY['queued'::text, 'running'::text, 'passed'::text, 'failed'::text, 'errored'::text])))
);

--
-- Name: TABLE agent_e2e_runs; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.agent_e2e_runs IS 'Per-environment E2E test-runner output. Drives the dev/staging/prod gate workflow.';

--
-- Name: agent_env_vars; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_env_vars (
    var_id uuid DEFAULT gen_random_uuid() NOT NULL,
    environment public.agent_environment NOT NULL,
    name text NOT NULL,
    value text,
    is_secret boolean DEFAULT false NOT NULL,
    source text DEFAULT 'db'::text NOT NULL,
    description text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid
);

--
-- Name: TABLE agent_env_vars; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.agent_env_vars IS 'Per-environment env-var registry. /admin/env-vars writes here; core.env_resolver.resolve_env() reads.';

--
-- Name: COLUMN agent_env_vars.is_secret; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.agent_env_vars.is_secret IS 'When true the admin UI masks the value in list views and never logs it via the audit trail.';

--
-- Name: COLUMN agent_env_vars.source; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.agent_env_vars.source IS 'How the value is sourced: ''db'' (inline), ''secret_manager:<project>/<secret>'', or ''supabase_vault:<key>''.';

--
-- Name: agent_eval_runs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_eval_runs (
    run_id uuid DEFAULT gen_random_uuid() NOT NULL,
    suite_id uuid,
    profile_id uuid NOT NULL,
    status public.agent_eval_run_status DEFAULT 'queued'::public.agent_eval_run_status NOT NULL,
    triggered_by uuid,
    results jsonb,
    score numeric,
    error text,
    started_at timestamp with time zone,
    finished_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    lmnr_trace_id text,
    context jsonb DEFAULT '{}'::jsonb NOT NULL,
    schedule_id uuid,
    bakeoff_id uuid,
    profile_version_id uuid
);

--
-- Name: COLUMN agent_eval_runs.lmnr_trace_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.agent_eval_runs.lmnr_trace_id IS 'Laminar root-span trace id for the suite run. Lets the admin portal deep-link an eval run row into the Laminar dashboard for drill-down.';

--
-- Name: agent_eval_schedules; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_eval_schedules (
    schedule_id uuid DEFAULT gen_random_uuid() NOT NULL,
    name text NOT NULL,
    suite_id uuid,
    profile_id uuid,
    agent_kind text,
    source text DEFAULT 'suite'::text NOT NULL,
    cadence text DEFAULT 'manual'::text NOT NULL,
    coverage jsonb DEFAULT '{}'::jsonb NOT NULL,
    is_enabled boolean DEFAULT true NOT NULL,
    last_run_at timestamp with time zone,
    next_run_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    CONSTRAINT agent_eval_schedules_cadence_check CHECK ((cadence = ANY (ARRAY['manual'::text, 'daily'::text, 'weekly'::text, 'monthly'::text]))),
    CONSTRAINT agent_eval_schedules_source_check CHECK ((source = ANY (ARRAY['suite'::text, 'live_chats'::text])))
);

--
-- Name: TABLE agent_eval_schedules; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.agent_eval_schedules IS 'Operator-defined eval jobs. source=suite runs synthetic scenarios; source=live_chats samples real ai_chat_sessions for reference-free quality judging.';

--
-- Name: agent_eval_suites; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_eval_suites (
    suite_id uuid DEFAULT gen_random_uuid() NOT NULL,
    name text NOT NULL,
    agent_kind public.ai_agent_kind NOT NULL,
    scenarios jsonb DEFAULT '[]'::jsonb NOT NULL,
    pass_threshold numeric DEFAULT 0.70 NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    evaluator_model_id uuid,
    default_context jsonb DEFAULT '{}'::jsonb NOT NULL,
    profile_id uuid,
    runner_kind text DEFAULT 'chat'::text NOT NULL
);

--
-- Name: COLUMN agent_eval_suites.evaluator_model_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.agent_eval_suites.evaluator_model_id IS 'Optional LLM judge for suites whose scenarios require qualitative scoring. NULL means use the suite''s substring/tool assertions only.';

--
-- Name: COLUMN agent_eval_suites.default_context; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.agent_eval_suites.default_context IS 'Default trigger context (business_id, branch_id, etc.) inherited by every run of this suite. Merged with per-run context at trigger time; per-run wins; per-scenario explicit fields win over both.';

--
-- Name: COLUMN agent_eval_suites.profile_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.agent_eval_suites.profile_id IS 'The one agent profile this suite grades. agent_kind is kept as a derived mirror (synced from this profile on write).';

--
-- Name: agent_mcp_servers; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_mcp_servers (
    mcp_server_id uuid DEFAULT gen_random_uuid() NOT NULL,
    name text NOT NULL,
    transport public.agent_mcp_transport,
    endpoint text,
    auth_env_var text,
    tool_subset text[],
    is_active boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    source text DEFAULT 'external'::text NOT NULL,
    is_builtin boolean DEFAULT false NOT NULL,
    context_fields jsonb DEFAULT '{}'::jsonb NOT NULL,
    usage_guidance text,
    endpoint_only boolean DEFAULT false NOT NULL,
    CONSTRAINT agent_mcp_servers_external_needs_endpoint CHECK (((source = 'bundle'::text) OR ((endpoint IS NOT NULL) AND (transport IS NOT NULL)))),
    CONSTRAINT agent_mcp_servers_source_check CHECK ((source = ANY (ARRAY['bundle'::text, 'external'::text])))
);

--
-- Name: COLUMN agent_mcp_servers.auth_env_var; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.agent_mcp_servers.auth_env_var IS 'Name of the env var holding the bearer/api token. Never store secrets in this table.';

--
-- Name: COLUMN agent_mcp_servers.source; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.agent_mcp_servers.source IS 'bundle = portal-composed collection of agent_tools rows; external = remote MCP server discovered via tools/list.';

--
-- Name: COLUMN agent_mcp_servers.context_fields; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.agent_mcp_servers.context_fields IS 'Server-level map of tool-arg name -> request-context ref (e.g. {"token": "$ctx.user_token"}). Args named here are stripped from model-facing schemas and injected server-side at call time.';

--
-- Name: agent_mcp_tools; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_mcp_tools (
    mcp_server_id uuid NOT NULL,
    tool_id uuid NOT NULL,
    "position" integer DEFAULT 0 NOT NULL,
    context_fields jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: TABLE agent_mcp_tools; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.agent_mcp_tools IS 'Bundle membership: which agent_tools rows a source=bundle MCP exposes. context_fields here overrides the server-level map for that member.';

--
-- Name: agent_models; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_models (
    model_id uuid DEFAULT gen_random_uuid() NOT NULL,
    name text NOT NULL,
    provider text NOT NULL,
    model_string text NOT NULL,
    temperature numeric DEFAULT 0.2 NOT NULL,
    max_output_tokens integer,
    extra_config jsonb DEFAULT '{}'::jsonb NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    environment public.agent_environment DEFAULT 'dev'::public.agent_environment NOT NULL
);

--
-- Name: TABLE agent_models; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.agent_models IS 'Reusable LLM model definitions referenced by agent_profiles.';

--
-- Name: COLUMN agent_models.environment; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.agent_models.environment IS 'Which environment this model row applies to. The agent_profile_versions snapshot pins the model_id at promote time, so changing this row never drifts a promoted profile.';

--
-- Name: agent_profile_mcp_servers; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_profile_mcp_servers (
    profile_id uuid NOT NULL,
    mcp_server_id uuid NOT NULL,
    tool_subset text[],
    "position" integer DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: COLUMN agent_profile_mcp_servers.tool_subset; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.agent_profile_mcp_servers.tool_subset IS 'NULL = expose the MCP''s full (current and future) tool set; non-NULL = pin exactly these python_names / MCP tool names.';

--
-- Name: agent_profile_skills; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_profile_skills (
    profile_id uuid NOT NULL,
    skill_id uuid NOT NULL,
    "position" integer DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: TABLE agent_profile_skills; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.agent_profile_skills IS 'Platform-skill attachments (twin of agent_profile_mcp_servers). Tenant skills (business_id set) are resolved dynamically and never appear here.';

--
-- Name: agent_profile_versions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_profile_versions (
    version_id uuid DEFAULT gen_random_uuid() NOT NULL,
    profile_id uuid NOT NULL,
    environment public.agent_environment NOT NULL,
    version_number integer NOT NULL,
    snapshot jsonb NOT NULL,
    promoted_by uuid,
    promoted_at timestamp with time zone DEFAULT now() NOT NULL,
    notes text,
    created_by uuid,
    updated_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: TABLE agent_profile_versions; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.agent_profile_versions IS 'Compile-on-promote snapshots. AgentProfileLoader reads the latest snapshot for env != dev so prompts/tools/models are pinned.';

--
-- Name: agent_profiles; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_profiles (
    profile_id uuid DEFAULT gen_random_uuid() NOT NULL,
    agent_kind public.ai_agent_kind NOT NULL,
    slug text NOT NULL,
    display_name text NOT NULL,
    description text,
    environment public.agent_environment DEFAULT 'dev'::public.agent_environment NOT NULL,
    model_main_id uuid NOT NULL,
    model_light_id uuid,
    prompt_version_ids jsonb DEFAULT '{}'::jsonb NOT NULL,
    tool_ids uuid[] DEFAULT ARRAY[]::uuid[] NOT NULL,
    data_source_ids uuid[] DEFAULT ARRAY[]::uuid[] NOT NULL,
    mcp_server_ids uuid[] DEFAULT ARRAY[]::uuid[] NOT NULL,
    escalation_policy jsonb DEFAULT '{}'::jsonb NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    endpoint_path text,
    downstream_profile_ids uuid[] DEFAULT ARRAY[]::uuid[] NOT NULL,
    parent_profile_id uuid,
    status text DEFAULT 'active'::text NOT NULL,
    input_validators jsonb DEFAULT '{}'::jsonb NOT NULL,
    voice_config jsonb DEFAULT '{}'::jsonb NOT NULL,
    memory_config jsonb DEFAULT '{}'::jsonb NOT NULL,
    delegatable boolean DEFAULT false NOT NULL,
    served_version_id uuid,
    CONSTRAINT agent_profiles_status_check CHECK ((status = ANY (ARRAY['active'::text, 'draft'::text, 'archived'::text, 'template'::text, 'specialist'::text])))
);

--
-- Name: TABLE agent_profiles; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.agent_profiles IS 'One row = one runnable agent configuration. agents look up their profile by (agent_kind, environment).';

--
-- Name: COLUMN agent_profiles.downstream_profile_ids; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.agent_profiles.downstream_profile_ids IS 'Profiles this one may delegate to (sub-agent links). Runtime resolves each id via AgentProfileLoader at request time.';

--
-- Name: COLUMN agent_profiles.input_validators; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.agent_profiles.input_validators IS 'Operator-curated request-body metadata: examples + notes + soft required-field overrides. The real schema is enforced by main.py''s Pydantic models — this column is operator documentation, surfaced in /admin/agents and copy-pasted into eval / dry-run forms.';

--
-- Name: COLUMN agent_profiles.voice_config; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.agent_profiles.voice_config IS 'Duplex voice settings, FLAT since 2026-08-17 (previously {turn-based keys..., live:{duplex keys...}}). Keys mirror the operator-set fields of voice/live/config.py::LiveVoiceConfig and are resolved by resolve_live_config(); see _VOICE_FIELDS for the allowlist. `_retired_turn_based` holds the pre-unification turn-based keys and is read by nothing.';

--
-- Name: COLUMN agent_profiles.memory_config; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.agent_profiles.memory_config IS 'Memory layer config: {enabled, extraction:{model_slot,importance_threshold,max_per_turn}, dedup:{similarity_threshold}, retrieval:{top_k,min_similarity,always_include_pinned}, summary:{trigger_every_n_turns,keep_recent_verbatim,model_slot}, retention_days}. Empty {} = disabled (defaults applied in code).';

--
-- Name: COLUMN agent_profiles.served_version_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.agent_profiles.served_version_id IS 'Per-(profile,environment) served-version pin. When set, AgentProfileLoader serves this exact agent_profile_versions snapshot instead of the default (dev/staging=live row, prod=latest promoted). NULL = default. Set from the agent config screen version bar / MCP; ON DELETE SET NULL so pruning a version reverts to default.';

--
-- Name: agent_prompt_versions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_prompt_versions (
    prompt_version_id uuid DEFAULT gen_random_uuid() NOT NULL,
    prompt_id uuid NOT NULL,
    version_number integer NOT NULL,
    content text NOT NULL,
    variables jsonb DEFAULT '[]'::jsonb NOT NULL,
    notes text,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid
);

--
-- Name: agent_prompts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_prompts (
    prompt_id uuid DEFAULT gen_random_uuid() NOT NULL,
    agent_kind public.ai_agent_kind NOT NULL,
    slug text NOT NULL,
    description text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid
);

--
-- Name: TABLE agent_prompts; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.agent_prompts IS 'A prompt is a named slot per agent kind (system, title, suggestion, etc).';

--
-- Name: agent_skill_resources; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_skill_resources (
    resource_id uuid DEFAULT gen_random_uuid() NOT NULL,
    skill_version_id uuid NOT NULL,
    filename text NOT NULL,
    content_type text DEFAULT 'text/markdown'::text NOT NULL,
    content text NOT NULL,
    "position" integer DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid
);

--
-- Name: TABLE agent_skill_resources; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.agent_skill_resources IS 'Bundled reference files (text/markdown only in this phase) per skill version, fetched via read_skill(slug, file). Large/binary assets are a GCS follow-up.';

--
-- Name: agent_skill_versions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_skill_versions (
    skill_version_id uuid DEFAULT gen_random_uuid() NOT NULL,
    skill_id uuid NOT NULL,
    version_number integer NOT NULL,
    content text NOT NULL,
    notes text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid
);

--
-- Name: TABLE agent_skill_versions; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.agent_skill_versions IS 'Versioned markdown bodies, twin of agent_prompt_versions. Runtime serves the latest version (prod platform skills: latest at promote time, frozen in the profile snapshot).';

--
-- Name: agent_skills; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_skills (
    skill_id uuid DEFAULT gen_random_uuid() NOT NULL,
    slug text NOT NULL,
    display_name text NOT NULL,
    description text NOT NULL,
    agent_kind public.ai_agent_kind NOT NULL,
    business_id uuid,
    always_on boolean DEFAULT false NOT NULL,
    linked_data_source_ids uuid[] DEFAULT ARRAY[]::uuid[] NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    status text DEFAULT 'active'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    CONSTRAINT agent_skills_status_check CHECK ((status = ANY (ARRAY['active'::text, 'draft'::text, 'archived'::text])))
);

--
-- Name: TABLE agent_skills; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.agent_skills IS 'Named, versioned instruction bundles (skills) for agents. business_id NULL = platform skill (attached to profiles via agent_profile_skills, frozen into prod snapshots); business_id set = tenant skill (resolved live per request by (agent_kind, business_id) — never profile-attached, never snapshot-frozen).';

--
-- Name: COLUMN agent_skills.slug; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.agent_skills.slug IS 'Stable lower-snake identifier the model uses with read_skill. Format ^[a-z][a-z0-9_-]{1,62}$ (enforced at the admin layer).';

--
-- Name: COLUMN agent_skills.description; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.agent_skills.description IS 'One-line catalog entry injected into the system prompt every turn. No { or } allowed (admin-layer validation) — it bypasses format_map but must not collide with downstream .format() passes.';

--
-- Name: COLUMN agent_skills.always_on; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.agent_skills.always_on IS 'When true the latest body is inlined into the system prompt every turn instead of waiting for a read_skill call. Cap of 10 always_on skills per resolved set enforced at runtime.';

--
-- Name: COLUMN agent_skills.linked_data_source_ids; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.agent_skills.linked_data_source_ids IS 'Optional agent_data_sources references. read_skill surfaces the source names so the skill can direct the agent at a specific knowledge base.';

--
-- Name: agent_test_runs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_test_runs (
    run_id uuid DEFAULT gen_random_uuid() NOT NULL,
    suite text NOT NULL,
    status text NOT NULL,
    pass_count integer,
    fail_count integer,
    skip_count integer,
    duration_ms integer,
    output text,
    results jsonb,
    error text,
    triggered_by uuid,
    started_at timestamp with time zone,
    finished_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by uuid,
    updated_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT agent_test_runs_status_check CHECK ((status = ANY (ARRAY['queued'::text, 'running'::text, 'passed'::text, 'failed'::text, 'errored'::text])))
);

--
-- Name: TABLE agent_test_runs; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.agent_test_runs IS 'Pytest invocations triggered from the admin portal /admin/tests screen.';

--
-- Name: agent_tools; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_tools (
    tool_id uuid DEFAULT gen_random_uuid() NOT NULL,
    python_name text NOT NULL,
    display_name text NOT NULL,
    description text NOT NULL,
    category text NOT NULL,
    scope public.agent_tool_scope NOT NULL,
    argument_schema jsonb DEFAULT '{}'::jsonb NOT NULL,
    source_file text NOT NULL,
    is_deprecated boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    agent_kind public.ai_agent_kind,
    additional_agent_kinds text[] DEFAULT ARRAY[]::text[] NOT NULL,
    kind text DEFAULT 'python'::text NOT NULL,
    endpoint_url text,
    http_method text DEFAULT 'POST'::text NOT NULL,
    auth_env_var text,
    timeout_seconds integer DEFAULT 30 NOT NULL,
    usage_guidance text,
    context_fields jsonb,
    CONSTRAINT agent_tools_endpoint_required_for_http CHECK (((kind = 'python'::text) OR (endpoint_url IS NOT NULL))),
    CONSTRAINT agent_tools_http_method_check CHECK ((http_method = ANY (ARRAY['GET'::text, 'POST'::text, 'PUT'::text, 'PATCH'::text, 'DELETE'::text]))),
    CONSTRAINT agent_tools_kind_check CHECK ((kind = ANY (ARRAY['python'::text, 'http'::text])))
);

--
-- Name: TABLE agent_tools; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.agent_tools IS 'Mirror of every Python function that can be exposed to an LLM via an agent profile.';

--
-- Name: COLUMN agent_tools.agent_kind; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.agent_tools.agent_kind IS 'Owning agent. Set by sync_tools at boot. Null only for legacy rows pre-2026-05-19.';

--
-- Name: COLUMN agent_tools.additional_agent_kinds; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.agent_tools.additional_agent_kinds IS 'Extra agent_kinds this tool is exposed to beyond the primary `agent_kind`.';

--
-- Name: COLUMN agent_tools.kind; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.agent_tools.kind IS 'Discriminator: ''python'' rows map to a @tool-decorated function discovered by sync_tools.py; ''http'' rows are operator-authored via the admin portal and resolved at request time via _http_adapter.build_http_tool.';

--
-- Name: COLUMN agent_tools.endpoint_url; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.agent_tools.endpoint_url IS 'Full URL the HTTP adapter calls. NULL for python tools; required when kind=''http'' (enforced by agent_tools_endpoint_required_for_http).';

--
-- Name: COLUMN agent_tools.http_method; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.agent_tools.http_method IS 'HTTP verb the adapter uses. GET sends validated kwargs as query params; POST/PUT/PATCH/DELETE send them as JSON body.';

--
-- Name: COLUMN agent_tools.auth_env_var; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.agent_tools.auth_env_var IS 'Name of an env var whose value is sent as ''Authorization: Bearer <value>''. NULL = no auth header. The env var is read at request time so secrets stay out of the DB.';

--
-- Name: COLUMN agent_tools.timeout_seconds; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.agent_tools.timeout_seconds IS 'Per-request httpx.Client timeout for HTTP tool calls. Default 30s.';

--
-- Name: agent_traces; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_traces (
    trace_id uuid DEFAULT gen_random_uuid() NOT NULL,
    session_id uuid,
    agent_kind public.ai_agent_kind NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    lmnr_trace_id text,
    input_tokens bigint,
    output_tokens bigint,
    cache_read_tokens bigint,
    cache_creation_tokens bigint,
    latency_ms bigint,
    model_id uuid,
    model_string text,
    cost_usd numeric,
    profile_id uuid,
    iteration_count integer,
    status text
);

--
-- Name: COLUMN agent_traces.lmnr_trace_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.agent_traces.lmnr_trace_id IS 'Laminar trace id captured at the moment the trace row was written. Used by the admin portal to deep-link rows into the Laminar dashboard.';

--
-- Name: COLUMN agent_traces.status; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.agent_traces.status IS 'Turn outcome: ''ok'' (completed) or ''error'' (failed mid-stream). NULL for legacy rows written before the column existed — treat NULL as ''ok''.';

--
-- Name: ai_background_leases; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ai_background_leases (
    name text NOT NULL,
    holder text,
    lease_until timestamp with time zone DEFAULT '1970-01-01 00:00:00+00'::timestamp with time zone NOT NULL,
    last_run_at timestamp with time zone,
    last_result jsonb
);

--
-- Name: ai_chat_messages; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ai_chat_messages (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    session_id uuid NOT NULL,
    role text NOT NULL,
    content text NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb,
    created_at timestamp with time zone DEFAULT now(),
    created_by uuid,
    updated_at timestamp with time zone DEFAULT now(),
    updated_by uuid,
    CONSTRAINT ai_chat_messages_role_check CHECK ((role = ANY (ARRAY['user'::text, 'assistant'::text, 'system'::text])))
);

--
-- Name: TABLE ai_chat_messages; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.ai_chat_messages IS 'Stores individual messages within chat sessions';

--
-- Name: COLUMN ai_chat_messages.metadata; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.ai_chat_messages.metadata IS 'Stores additional data like thoughts_count, tools_used, table_data, chart_data';

--
-- Name: ai_chat_sessions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ai_chat_sessions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid,
    business_id uuid,
    branch_id uuid,
    title text DEFAULT 'New Chat'::text NOT NULL,
    message_count integer DEFAULT 0,
    metadata jsonb DEFAULT '{}'::jsonb,
    created_at timestamp with time zone DEFAULT now(),
    created_by uuid,
    updated_at timestamp with time zone DEFAULT now(),
    updated_by uuid,
    deleted_at timestamp with time zone,
    agent_kind public.ai_agent_kind,
    running_summary text,
    summary_message_count integer DEFAULT 0 NOT NULL,
    summary_updated_at timestamp with time zone
);

--
-- Name: TABLE ai_chat_sessions; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.ai_chat_sessions IS 'Stores AI chat session metadata with RLS enforced per user. Supports soft deletes via deleted_at column';

--
-- Name: COLUMN ai_chat_sessions.message_count; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.ai_chat_sessions.message_count IS 'Cached count of messages in this session';

--
-- Name: COLUMN ai_chat_sessions.deleted_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.ai_chat_sessions.deleted_at IS 'Timestamp for soft deletes. NULL means active session';

--
-- Name: COLUMN ai_chat_sessions.agent_kind; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.ai_chat_sessions.agent_kind IS 'Which AI agent owns this session. NULL for legacy rows that predate the discriminator.';

--
-- Name: COLUMN ai_chat_sessions.running_summary; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.ai_chat_sessions.running_summary IS 'Rolling summary of older turns (short-term memory). Refreshed by the light model when message count crosses memory_config.summary.trigger_every_n_turns.';

--
-- Name: COLUMN ai_chat_sessions.summary_message_count; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.ai_chat_sessions.summary_message_count IS 'Number of messages the running_summary already covers. The summarizer refreshes when newer messages exceed this by memory_config.summary.trigger_every_n_turns.';

--
-- Name: COLUMN ai_chat_sessions.summary_updated_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.ai_chat_sessions.summary_updated_at IS 'When running_summary was last refreshed.';

--
-- Name: ai_conversation_insights; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ai_conversation_insights (
    session_id uuid NOT NULL,
    agent_kind text,
    environment text,
    channel text DEFAULT 'chat'::text NOT NULL,
    outcome text NOT NULL,
    outcome_source text DEFAULT 'rule'::text NOT NULL,
    topic text,
    language text,
    user_turns integer DEFAULT 0 NOT NULL,
    message_count integer DEFAULT 0 NOT NULL,
    is_simulated boolean DEFAULT false NOT NULL,
    started_at timestamp with time zone,
    last_message_at timestamp with time zone,
    classifier_model text,
    version smallint DEFAULT 1 NOT NULL,
    analyzed_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ai_conversation_insights_channel_check CHECK ((channel = ANY (ARRAY['chat'::text, 'voice'::text]))),
    CONSTRAINT ai_conversation_insights_outcome_check CHECK ((outcome = ANY (ARRAY['resolved'::text, 'abandoned'::text, 'escalated'::text, 'unknown'::text]))),
    CONSTRAINT ai_conversation_insights_outcome_source_check CHECK ((outcome_source = ANY (ARRAY['telemetry'::text, 'llm'::text, 'rule'::text])))
);

--
-- Name: TABLE ai_conversation_insights; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.ai_conversation_insights IS 'Derived per-conversation outcome/topic/language for the admin Overview. Written by the background sweep in core/conversation_insights.py; safe to truncate (the sweep rebuilds the last 30 days).';

--
-- Name: ai_kb_embeddings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ai_kb_embeddings (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    source_id uuid NOT NULL,
    page_id uuid NOT NULL,
    content text NOT NULL,
    heading text,
    slug text,
    token_count integer,
    embedding public.vector(1536),
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: ai_kb_pages; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ai_kb_pages (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    source_id uuid NOT NULL,
    title text NOT NULL,
    type text DEFAULT 'document'::text NOT NULL,
    language text DEFAULT 'en'::text NOT NULL,
    content text,
    source text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by uuid,
    checksum text,
    path text,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL
);

--
-- Name: ai_user_memory; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ai_user_memory (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    business_id uuid NOT NULL,
    agent_kind public.ai_agent_kind NOT NULL,
    content text NOT NULL,
    embedding public.vector(768),
    importance real DEFAULT 0.5 NOT NULL,
    pinned boolean DEFAULT false NOT NULL,
    source text DEFAULT 'auto'::text NOT NULL,
    status text DEFAULT 'active'::text NOT NULL,
    origin_session_id uuid,
    origin_message_id uuid,
    use_count integer DEFAULT 0 NOT NULL,
    last_used_at timestamp with time zone,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ai_user_memory_source_check CHECK ((source = ANY (ARRAY['auto'::text, 'manual'::text, 'tool'::text]))),
    CONSTRAINT ai_user_memory_status_check CHECK ((status = ANY (ARRAY['active'::text, 'archived'::text])))
);

--
-- Name: TABLE ai_user_memory; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.ai_user_memory IS 'Per-user durable memory facts. Auto-extracted after each turn, embedded (Gemini text-embedding-004, 768d), retrieved by cosine similarity. Namespaced by agent_kind. No RLS — service-role only, like ai_chat_*.';

--
-- Name: ai_voice_budgets; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ai_voice_budgets (
    business_id uuid NOT NULL,
    daily_seconds integer NOT NULL,
    note text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ai_voice_budgets_daily_seconds_check CHECK ((daily_seconds >= 0))
);

--
-- Name: ai_voice_recording_chunks; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ai_voice_recording_chunks (
    recording_id uuid NOT NULL,
    seq integer NOT NULL,
    audio_b64 text NOT NULL
);

--
-- Name: TABLE ai_voice_recording_chunks; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.ai_voice_recording_chunks IS 'Base64 slices of one WAV file, ordered by seq (chunk 0 starts with the RIFF header) — concatenating every decoded chunk in seq order yields the complete playable file. Chunked because a multi-minute stereo WAV exceeds what one PostgREST insert comfortably carries. No FK on purpose (loose reference like the rest of the AI project); the recorder deletes chunks with their recording row on prune.';

--
-- Name: ai_voice_recordings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ai_voice_recordings (
    recording_id uuid DEFAULT gen_random_uuid() NOT NULL,
    call_id uuid NOT NULL,
    session_id uuid NOT NULL,
    agent_kind text NOT NULL,
    business_id uuid,
    provider text,
    simulated boolean DEFAULT false NOT NULL,
    sample_rate integer NOT NULL,
    channels integer DEFAULT 2 NOT NULL,
    duration_ms bigint DEFAULT 0 NOT NULL,
    size_bytes bigint DEFAULT 0 NOT NULL,
    chunk_count integer DEFAULT 0 NOT NULL,
    status text DEFAULT 'complete'::text NOT NULL,
    note text,
    started_at timestamp with time zone NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: ai_voice_scenario_runs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ai_voice_scenario_runs (
    run_id uuid DEFAULT gen_random_uuid() NOT NULL,
    scenario_id uuid NOT NULL,
    slug text NOT NULL,
    status text DEFAULT 'running'::text NOT NULL,
    target text,
    detail jsonb DEFAULT '{}'::jsonb NOT NULL,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    finished_at timestamp with time zone
);

--
-- Name: TABLE ai_voice_scenario_runs; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.ai_voice_scenario_runs IS 'One row per voice-scenario execution. status: running|passed|failed|error|skipped (skipped = missing TTS key or disabled scenario). detail carries per-step results and the captured frame timeline tail.';

--
-- Name: ai_voice_scenarios; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ai_voice_scenarios (
    scenario_id uuid DEFAULT gen_random_uuid() NOT NULL,
    slug text NOT NULL,
    agent_kind text NOT NULL,
    title text,
    enabled boolean DEFAULT true NOT NULL,
    ws_path text DEFAULT '/voice/live'::text NOT NULL,
    locale text,
    steps jsonb DEFAULT '[]'::jsonb NOT NULL,
    notes text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: ai_voice_usage_daily; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ai_voice_usage_daily (
    business_id uuid NOT NULL,
    usage_date date NOT NULL,
    seconds_used integer DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: TABLE ai_voice_usage_daily; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.ai_voice_usage_daily IS 'Per-tenant per-day duplex-voice audio seconds. Written by voice/live/budget.py: a handshake advance at session start, incremental deltas periodically during a session (voice_config.live.budget_charge_interval_sec), and the uncharged remainder at teardown.';

--
-- Name: test_fixtures; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.test_fixtures (
    fixture_id uuid DEFAULT gen_random_uuid() NOT NULL,
    name text NOT NULL,
    environment text DEFAULT 'dev'::text NOT NULL,
    kind text DEFAULT 'string'::text NOT NULL,
    value text NOT NULL,
    description text,
    last_used_at timestamp with time zone,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    CONSTRAINT test_fixtures_environment_check CHECK ((environment = ANY (ARRAY['dev'::text, 'staging'::text, 'prod'::text]))),
    CONSTRAINT test_fixtures_kind_check CHECK ((kind = ANY (ARRAY['jwt'::text, 'uuid'::text, 'string'::text, 'json'::text, 'secret'::text])))
);

--
-- Name: TABLE test_fixtures; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.test_fixtures IS 'Operator-managed test inputs: JWTs, tenant IDs, branch IDs, anything pytest needs to exercise auth-gated paths. Source of truth for the WAJ_TEST_* env vars exported via /admin/test-fixtures/export.';

--
-- Name: COLUMN test_fixtures.kind; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.test_fixtures.kind IS 'Type hint for the UI: jwt/secret hide the value behind reveal; uuid validates UUID shape; json renders as code block; string is the catch-all.';

--
-- Name: admin_jobs admin_jobs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.admin_jobs
    ADD CONSTRAINT admin_jobs_pkey PRIMARY KEY (job_id);

--
-- Name: agent_audit_log agent_audit_log_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_audit_log
    ADD CONSTRAINT agent_audit_log_pkey PRIMARY KEY (audit_id);

--
-- Name: agent_bakeoffs agent_bakeoffs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_bakeoffs
    ADD CONSTRAINT agent_bakeoffs_pkey PRIMARY KEY (bakeoff_id);

--
-- Name: agent_channels agent_channels_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_channels
    ADD CONSTRAINT agent_channels_pkey PRIMARY KEY (channel_id);

--
-- Name: agent_channels agent_channels_slug_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_channels
    ADD CONSTRAINT agent_channels_slug_key UNIQUE (slug);

--
-- Name: agent_data_sources agent_data_sources_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_data_sources
    ADD CONSTRAINT agent_data_sources_name_key UNIQUE (name);

--
-- Name: agent_data_sources agent_data_sources_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_data_sources
    ADD CONSTRAINT agent_data_sources_pkey PRIMARY KEY (source_id);

--
-- Name: agent_deployment_targets agent_deployment_targets_environment_label_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_deployment_targets
    ADD CONSTRAINT agent_deployment_targets_environment_label_key UNIQUE (environment, label);

--
-- Name: agent_deployment_targets agent_deployment_targets_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_deployment_targets
    ADD CONSTRAINT agent_deployment_targets_pkey PRIMARY KEY (target_id);

--
-- Name: agent_e2e_runs agent_e2e_runs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_e2e_runs
    ADD CONSTRAINT agent_e2e_runs_pkey PRIMARY KEY (run_id);

--
-- Name: agent_env_vars agent_env_vars_environment_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_env_vars
    ADD CONSTRAINT agent_env_vars_environment_name_key UNIQUE (environment, name);

--
-- Name: agent_env_vars agent_env_vars_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_env_vars
    ADD CONSTRAINT agent_env_vars_pkey PRIMARY KEY (var_id);

--
-- Name: agent_eval_runs agent_eval_runs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_eval_runs
    ADD CONSTRAINT agent_eval_runs_pkey PRIMARY KEY (run_id);

--
-- Name: agent_eval_schedules agent_eval_schedules_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_eval_schedules
    ADD CONSTRAINT agent_eval_schedules_pkey PRIMARY KEY (schedule_id);

--
-- Name: agent_eval_suites agent_eval_suites_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_eval_suites
    ADD CONSTRAINT agent_eval_suites_pkey PRIMARY KEY (suite_id);

--
-- Name: agent_mcp_servers agent_mcp_servers_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_mcp_servers
    ADD CONSTRAINT agent_mcp_servers_name_key UNIQUE (name);

--
-- Name: agent_mcp_servers agent_mcp_servers_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_mcp_servers
    ADD CONSTRAINT agent_mcp_servers_pkey PRIMARY KEY (mcp_server_id);

--
-- Name: agent_mcp_tools agent_mcp_tools_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_mcp_tools
    ADD CONSTRAINT agent_mcp_tools_pkey PRIMARY KEY (mcp_server_id, tool_id);

--
-- Name: agent_models agent_models_name_env_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_models
    ADD CONSTRAINT agent_models_name_env_key UNIQUE (name, environment);

--
-- Name: agent_models agent_models_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_models
    ADD CONSTRAINT agent_models_pkey PRIMARY KEY (model_id);

--
-- Name: agent_profile_mcp_servers agent_profile_mcp_servers_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_profile_mcp_servers
    ADD CONSTRAINT agent_profile_mcp_servers_pkey PRIMARY KEY (profile_id, mcp_server_id);

--
-- Name: agent_profile_skills agent_profile_skills_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_profile_skills
    ADD CONSTRAINT agent_profile_skills_pkey PRIMARY KEY (profile_id, skill_id);

--
-- Name: agent_profile_versions agent_profile_versions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_profile_versions
    ADD CONSTRAINT agent_profile_versions_pkey PRIMARY KEY (version_id);

--
-- Name: agent_profile_versions agent_profile_versions_profile_id_environment_version_numbe_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_profile_versions
    ADD CONSTRAINT agent_profile_versions_profile_id_environment_version_numbe_key UNIQUE (profile_id, environment, version_number);

--
-- Name: agent_profiles agent_profiles_agent_kind_slug_environment_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_profiles
    ADD CONSTRAINT agent_profiles_agent_kind_slug_environment_key UNIQUE (agent_kind, slug, environment);

--
-- Name: agent_profiles agent_profiles_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_profiles
    ADD CONSTRAINT agent_profiles_pkey PRIMARY KEY (profile_id);

--
-- Name: agent_prompt_versions agent_prompt_versions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_prompt_versions
    ADD CONSTRAINT agent_prompt_versions_pkey PRIMARY KEY (prompt_version_id);

--
-- Name: agent_prompt_versions agent_prompt_versions_prompt_id_version_number_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_prompt_versions
    ADD CONSTRAINT agent_prompt_versions_prompt_id_version_number_key UNIQUE (prompt_id, version_number);

--
-- Name: agent_prompts agent_prompts_agent_kind_slug_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_prompts
    ADD CONSTRAINT agent_prompts_agent_kind_slug_key UNIQUE (agent_kind, slug);

--
-- Name: agent_prompts agent_prompts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_prompts
    ADD CONSTRAINT agent_prompts_pkey PRIMARY KEY (prompt_id);

--
-- Name: agent_skill_resources agent_skill_resources_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_skill_resources
    ADD CONSTRAINT agent_skill_resources_pkey PRIMARY KEY (resource_id);

--
-- Name: agent_skill_resources agent_skill_resources_version_filename_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_skill_resources
    ADD CONSTRAINT agent_skill_resources_version_filename_key UNIQUE (skill_version_id, filename);

--
-- Name: agent_skill_versions agent_skill_versions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_skill_versions
    ADD CONSTRAINT agent_skill_versions_pkey PRIMARY KEY (skill_version_id);

--
-- Name: agent_skill_versions agent_skill_versions_skill_id_version_number_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_skill_versions
    ADD CONSTRAINT agent_skill_versions_skill_id_version_number_key UNIQUE (skill_id, version_number);

--
-- Name: agent_skills agent_skills_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_skills
    ADD CONSTRAINT agent_skills_pkey PRIMARY KEY (skill_id);

--
-- Name: agent_test_runs agent_test_runs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_test_runs
    ADD CONSTRAINT agent_test_runs_pkey PRIMARY KEY (run_id);

--
-- Name: agent_tools agent_tools_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_tools
    ADD CONSTRAINT agent_tools_pkey PRIMARY KEY (tool_id);

--
-- Name: agent_tools agent_tools_python_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_tools
    ADD CONSTRAINT agent_tools_python_name_key UNIQUE (python_name);

--
-- Name: agent_traces agent_traces_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_traces
    ADD CONSTRAINT agent_traces_pkey PRIMARY KEY (trace_id);

--
-- Name: ai_background_leases ai_background_leases_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_background_leases
    ADD CONSTRAINT ai_background_leases_pkey PRIMARY KEY (name);

--
-- Name: ai_chat_messages ai_chat_messages_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_chat_messages
    ADD CONSTRAINT ai_chat_messages_pkey PRIMARY KEY (id);

--
-- Name: ai_chat_sessions ai_chat_sessions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_chat_sessions
    ADD CONSTRAINT ai_chat_sessions_pkey PRIMARY KEY (id);

--
-- Name: ai_conversation_insights ai_conversation_insights_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_conversation_insights
    ADD CONSTRAINT ai_conversation_insights_pkey PRIMARY KEY (session_id);

--
-- Name: ai_kb_embeddings ai_kb_embeddings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_kb_embeddings
    ADD CONSTRAINT ai_kb_embeddings_pkey PRIMARY KEY (id);

--
-- Name: ai_kb_pages ai_kb_pages_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_kb_pages
    ADD CONSTRAINT ai_kb_pages_pkey PRIMARY KEY (id);

--
-- Name: ai_user_memory ai_user_memory_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_user_memory
    ADD CONSTRAINT ai_user_memory_pkey PRIMARY KEY (id);

--
-- Name: ai_voice_budgets ai_voice_budgets_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_voice_budgets
    ADD CONSTRAINT ai_voice_budgets_pkey PRIMARY KEY (business_id);

--
-- Name: ai_voice_recording_chunks ai_voice_recording_chunks_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_voice_recording_chunks
    ADD CONSTRAINT ai_voice_recording_chunks_pkey PRIMARY KEY (recording_id, seq);

--
-- Name: ai_voice_recordings ai_voice_recordings_call_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_voice_recordings
    ADD CONSTRAINT ai_voice_recordings_call_id_key UNIQUE (call_id);

--
-- Name: ai_voice_recordings ai_voice_recordings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_voice_recordings
    ADD CONSTRAINT ai_voice_recordings_pkey PRIMARY KEY (recording_id);

--
-- Name: ai_voice_scenario_runs ai_voice_scenario_runs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_voice_scenario_runs
    ADD CONSTRAINT ai_voice_scenario_runs_pkey PRIMARY KEY (run_id);

--
-- Name: ai_voice_scenarios ai_voice_scenarios_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_voice_scenarios
    ADD CONSTRAINT ai_voice_scenarios_pkey PRIMARY KEY (scenario_id);

--
-- Name: ai_voice_scenarios ai_voice_scenarios_slug_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_voice_scenarios
    ADD CONSTRAINT ai_voice_scenarios_slug_key UNIQUE (slug);

--
-- Name: ai_voice_usage_daily ai_voice_usage_daily_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_voice_usage_daily
    ADD CONSTRAINT ai_voice_usage_daily_pkey PRIMARY KEY (business_id, usage_date);

--
-- Name: test_fixtures test_fixtures_name_environment_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.test_fixtures
    ADD CONSTRAINT test_fixtures_name_environment_key UNIQUE (name, environment);

--
-- Name: test_fixtures test_fixtures_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.test_fixtures
    ADD CONSTRAINT test_fixtures_pkey PRIMARY KEY (fixture_id);

--
-- Name: ai_conversation_insights_kind_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ai_conversation_insights_kind_idx ON public.ai_conversation_insights USING btree (agent_kind, last_message_at DESC);

--
-- Name: ai_conversation_insights_last_msg_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ai_conversation_insights_last_msg_idx ON public.ai_conversation_insights USING btree (last_message_at DESC);

--
-- Name: ai_kb_embeddings_embedding_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ai_kb_embeddings_embedding_idx ON public.ai_kb_embeddings USING hnsw (embedding public.vector_cosine_ops);

--
-- Name: ai_kb_embeddings_source_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ai_kb_embeddings_source_id_idx ON public.ai_kb_embeddings USING btree (source_id);

--
-- Name: ai_kb_pages_source_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ai_kb_pages_source_id_idx ON public.ai_kb_pages USING btree (source_id);

--
-- Name: ai_kb_pages_source_path_key; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ai_kb_pages_source_path_key ON public.ai_kb_pages USING btree (source_id, path) WHERE (path IS NOT NULL);

--
-- Name: ai_voice_recordings_created_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ai_voice_recordings_created_idx ON public.ai_voice_recordings USING btree (created_at);

--
-- Name: ai_voice_recordings_session_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ai_voice_recordings_session_idx ON public.ai_voice_recordings USING btree (session_id, started_at DESC);

--
-- Name: ai_voice_scenario_runs_slug_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ai_voice_scenario_runs_slug_idx ON public.ai_voice_scenario_runs USING btree (slug, started_at DESC);

--
-- Name: idx_admin_jobs_kind; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_admin_jobs_kind ON public.admin_jobs USING btree (kind, created_at DESC);

--
-- Name: idx_admin_jobs_related_entity; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_admin_jobs_related_entity ON public.admin_jobs USING btree (related_entity_type, related_entity_id, status);

--
-- Name: idx_admin_jobs_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_admin_jobs_status ON public.admin_jobs USING btree (status, created_at DESC);

--
-- Name: idx_agent_audit_log_actor; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_audit_log_actor ON public.agent_audit_log USING btree (actor_user_id, created_at DESC);

--
-- Name: idx_agent_audit_log_entity; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_audit_log_entity ON public.agent_audit_log USING btree (entity_type, entity_id, created_at DESC);

--
-- Name: idx_agent_bakeoffs_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_bakeoffs_created ON public.agent_bakeoffs USING btree (created_at DESC);

--
-- Name: idx_agent_bakeoffs_profile; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_bakeoffs_profile ON public.agent_bakeoffs USING btree (profile_id);

--
-- Name: idx_agent_channels_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_channels_active ON public.agent_channels USING btree (is_active) WHERE is_active;

--
-- Name: idx_agent_channels_business_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_channels_business_id ON public.agent_channels USING btree (business_id);

--
-- Name: idx_agent_channels_default_profile; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_channels_default_profile ON public.agent_channels USING btree (default_profile_id) WHERE (default_profile_id IS NOT NULL);

--
-- Name: idx_agent_channels_kind; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_channels_kind ON public.agent_channels USING btree (kind);

--
-- Name: idx_agent_data_sources_agent_kind; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_data_sources_agent_kind ON public.agent_data_sources USING btree (agent_kind);

--
-- Name: idx_agent_deployment_targets_env; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_deployment_targets_env ON public.agent_deployment_targets USING btree (environment, is_default DESC, is_active);

--
-- Name: idx_agent_e2e_runs_lookup; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_e2e_runs_lookup ON public.agent_e2e_runs USING btree (profile_id, environment, created_at DESC);

--
-- Name: idx_agent_e2e_runs_suite; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_e2e_runs_suite ON public.agent_e2e_runs USING btree (suite_id) WHERE (suite_id IS NOT NULL);

--
-- Name: idx_agent_env_vars_env_name; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_env_vars_env_name ON public.agent_env_vars USING btree (environment, name);

--
-- Name: idx_agent_eval_runs_bakeoff; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_eval_runs_bakeoff ON public.agent_eval_runs USING btree (bakeoff_id) WHERE (bakeoff_id IS NOT NULL);

--
-- Name: idx_agent_eval_runs_profile_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_eval_runs_profile_created ON public.agent_eval_runs USING btree (profile_id, created_at DESC);

--
-- Name: idx_agent_eval_runs_schedule; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_eval_runs_schedule ON public.agent_eval_runs USING btree (schedule_id) WHERE (schedule_id IS NOT NULL);

--
-- Name: idx_agent_eval_runs_suite; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_eval_runs_suite ON public.agent_eval_runs USING btree (suite_id);

--
-- Name: idx_agent_eval_suites_evaluator_model; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_eval_suites_evaluator_model ON public.agent_eval_suites USING btree (evaluator_model_id) WHERE (evaluator_model_id IS NOT NULL);

--
-- Name: idx_agent_eval_suites_profile; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_eval_suites_profile ON public.agent_eval_suites USING btree (profile_id);

--
-- Name: idx_agent_models_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_models_active ON public.agent_models USING btree (provider, is_active);

--
-- Name: idx_agent_models_environment; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_models_environment ON public.agent_models USING btree (environment, is_active);

--
-- Name: idx_agent_profile_versions_lookup; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_profile_versions_lookup ON public.agent_profile_versions USING btree (profile_id, environment, version_number DESC);

--
-- Name: idx_agent_profiles_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_profiles_active ON public.agent_profiles USING btree (agent_kind, environment, is_active);

--
-- Name: idx_agent_profiles_kind_env_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_profiles_kind_env_status ON public.agent_profiles USING btree (agent_kind, environment, status);

--
-- Name: idx_agent_profiles_model_light; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_profiles_model_light ON public.agent_profiles USING btree (model_light_id) WHERE (model_light_id IS NOT NULL);

--
-- Name: idx_agent_profiles_model_main; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_profiles_model_main ON public.agent_profiles USING btree (model_main_id);

--
-- Name: idx_agent_profiles_parent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_profiles_parent ON public.agent_profiles USING btree (parent_profile_id);

--
-- Name: idx_agent_prompt_versions_prompt; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_prompt_versions_prompt ON public.agent_prompt_versions USING btree (prompt_id, version_number DESC);

--
-- Name: idx_agent_skill_versions_skill; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_skill_versions_skill ON public.agent_skill_versions USING btree (skill_id, version_number DESC);

--
-- Name: idx_agent_skills_lookup; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_skills_lookup ON public.agent_skills USING btree (agent_kind, business_id, is_active);

--
-- Name: idx_agent_skills_platform_slug; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_agent_skills_platform_slug ON public.agent_skills USING btree (agent_kind, slug) WHERE (business_id IS NULL);

--
-- Name: idx_agent_skills_tenant_slug; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_agent_skills_tenant_slug ON public.agent_skills USING btree (agent_kind, business_id, slug) WHERE (business_id IS NOT NULL);

--
-- Name: idx_agent_test_runs_recent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_test_runs_recent ON public.agent_test_runs USING btree (created_at DESC);

--
-- Name: idx_agent_tools_agent_kind; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_tools_agent_kind ON public.agent_tools USING btree (agent_kind);

--
-- Name: idx_agent_tools_category; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_tools_category ON public.agent_tools USING btree (category, is_deprecated);

--
-- Name: idx_agent_traces_agent_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_traces_agent_created ON public.agent_traces USING btree (agent_kind, created_at DESC);

--
-- Name: idx_agent_traces_agent_kind; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_traces_agent_kind ON public.agent_traces USING btree (agent_kind);

--
-- Name: idx_agent_traces_created_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_traces_created_at ON public.agent_traces USING btree (created_at);

--
-- Name: idx_agent_traces_session; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_traces_session ON public.agent_traces USING btree (session_id);

--
-- Name: idx_ai_chat_sessions_agent_kind; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ai_chat_sessions_agent_kind ON public.ai_chat_sessions USING btree (agent_kind) WHERE (agent_kind IS NOT NULL);

--
-- Name: idx_ai_user_memory_business; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ai_user_memory_business ON public.ai_user_memory USING btree (business_id, agent_kind);

--
-- Name: idx_ai_user_memory_embedding; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ai_user_memory_embedding ON public.ai_user_memory USING hnsw (embedding public.vector_cosine_ops);

--
-- Name: idx_ai_user_memory_lookup; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ai_user_memory_lookup ON public.ai_user_memory USING btree (user_id, agent_kind, status);

--
-- Name: idx_chat_messages_created_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_chat_messages_created_at ON public.ai_chat_messages USING btree (created_at);

--
-- Name: idx_chat_messages_session_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_chat_messages_session_id ON public.ai_chat_messages USING btree (session_id);

--
-- Name: idx_chat_sessions_business_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_chat_sessions_business_id ON public.ai_chat_sessions USING btree (business_id);

--
-- Name: idx_chat_sessions_created_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_chat_sessions_created_at ON public.ai_chat_sessions USING btree (created_at DESC);

--
-- Name: idx_chat_sessions_deleted_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_chat_sessions_deleted_at ON public.ai_chat_sessions USING btree (deleted_at);

--
-- Name: idx_chat_sessions_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_chat_sessions_user_id ON public.ai_chat_sessions USING btree (user_id);

--
-- Name: idx_eval_schedules_enabled; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_eval_schedules_enabled ON public.agent_eval_schedules USING btree (is_enabled, next_run_at);

--
-- Name: idx_test_fixtures_env; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_test_fixtures_env ON public.test_fixtures USING btree (environment);

--
-- Name: agent_bakeoffs agent_bakeoffs_profile_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_bakeoffs
    ADD CONSTRAINT agent_bakeoffs_profile_id_fkey FOREIGN KEY (profile_id) REFERENCES public.agent_profiles(profile_id) ON DELETE CASCADE;

--
-- Name: agent_channels agent_channels_default_profile_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_channels
    ADD CONSTRAINT agent_channels_default_profile_id_fkey FOREIGN KEY (default_profile_id) REFERENCES public.agent_profiles(profile_id) ON DELETE SET NULL;

--
-- Name: agent_e2e_runs agent_e2e_runs_profile_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_e2e_runs
    ADD CONSTRAINT agent_e2e_runs_profile_id_fkey FOREIGN KEY (profile_id) REFERENCES public.agent_profiles(profile_id) ON DELETE CASCADE;

--
-- Name: agent_e2e_runs agent_e2e_runs_suite_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_e2e_runs
    ADD CONSTRAINT agent_e2e_runs_suite_id_fkey FOREIGN KEY (suite_id) REFERENCES public.agent_eval_suites(suite_id) ON DELETE SET NULL;

--
-- Name: agent_eval_runs agent_eval_runs_bakeoff_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_eval_runs
    ADD CONSTRAINT agent_eval_runs_bakeoff_id_fkey FOREIGN KEY (bakeoff_id) REFERENCES public.agent_bakeoffs(bakeoff_id) ON DELETE SET NULL;

--
-- Name: agent_eval_runs agent_eval_runs_profile_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_eval_runs
    ADD CONSTRAINT agent_eval_runs_profile_id_fkey FOREIGN KEY (profile_id) REFERENCES public.agent_profiles(profile_id) ON DELETE CASCADE;

--
-- Name: agent_eval_runs agent_eval_runs_profile_version_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_eval_runs
    ADD CONSTRAINT agent_eval_runs_profile_version_id_fkey FOREIGN KEY (profile_version_id) REFERENCES public.agent_profile_versions(version_id) ON DELETE SET NULL;

--
-- Name: agent_eval_runs agent_eval_runs_schedule_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_eval_runs
    ADD CONSTRAINT agent_eval_runs_schedule_id_fkey FOREIGN KEY (schedule_id) REFERENCES public.agent_eval_schedules(schedule_id) ON DELETE SET NULL;

--
-- Name: agent_eval_runs agent_eval_runs_suite_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_eval_runs
    ADD CONSTRAINT agent_eval_runs_suite_id_fkey FOREIGN KEY (suite_id) REFERENCES public.agent_eval_suites(suite_id) ON DELETE CASCADE;

--
-- Name: agent_eval_schedules agent_eval_schedules_profile_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_eval_schedules
    ADD CONSTRAINT agent_eval_schedules_profile_id_fkey FOREIGN KEY (profile_id) REFERENCES public.agent_profiles(profile_id) ON DELETE SET NULL;

--
-- Name: agent_eval_schedules agent_eval_schedules_suite_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_eval_schedules
    ADD CONSTRAINT agent_eval_schedules_suite_id_fkey FOREIGN KEY (suite_id) REFERENCES public.agent_eval_suites(suite_id) ON DELETE CASCADE;

--
-- Name: agent_eval_suites agent_eval_suites_evaluator_model_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_eval_suites
    ADD CONSTRAINT agent_eval_suites_evaluator_model_id_fkey FOREIGN KEY (evaluator_model_id) REFERENCES public.agent_models(model_id) ON DELETE SET NULL;

--
-- Name: agent_eval_suites agent_eval_suites_profile_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_eval_suites
    ADD CONSTRAINT agent_eval_suites_profile_id_fkey FOREIGN KEY (profile_id) REFERENCES public.agent_profiles(profile_id) ON DELETE SET NULL;

--
-- Name: agent_mcp_tools agent_mcp_tools_mcp_server_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_mcp_tools
    ADD CONSTRAINT agent_mcp_tools_mcp_server_id_fkey FOREIGN KEY (mcp_server_id) REFERENCES public.agent_mcp_servers(mcp_server_id) ON DELETE CASCADE;

--
-- Name: agent_mcp_tools agent_mcp_tools_tool_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_mcp_tools
    ADD CONSTRAINT agent_mcp_tools_tool_id_fkey FOREIGN KEY (tool_id) REFERENCES public.agent_tools(tool_id) ON DELETE CASCADE;

--
-- Name: agent_profile_mcp_servers agent_profile_mcp_servers_mcp_server_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_profile_mcp_servers
    ADD CONSTRAINT agent_profile_mcp_servers_mcp_server_id_fkey FOREIGN KEY (mcp_server_id) REFERENCES public.agent_mcp_servers(mcp_server_id) ON DELETE CASCADE;

--
-- Name: agent_profile_mcp_servers agent_profile_mcp_servers_profile_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_profile_mcp_servers
    ADD CONSTRAINT agent_profile_mcp_servers_profile_id_fkey FOREIGN KEY (profile_id) REFERENCES public.agent_profiles(profile_id) ON DELETE CASCADE;

--
-- Name: agent_profile_skills agent_profile_skills_profile_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_profile_skills
    ADD CONSTRAINT agent_profile_skills_profile_fkey FOREIGN KEY (profile_id) REFERENCES public.agent_profiles(profile_id) ON DELETE CASCADE;

--
-- Name: agent_profile_skills agent_profile_skills_skill_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_profile_skills
    ADD CONSTRAINT agent_profile_skills_skill_fkey FOREIGN KEY (skill_id) REFERENCES public.agent_skills(skill_id) ON DELETE CASCADE;

--
-- Name: agent_profile_versions agent_profile_versions_profile_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_profile_versions
    ADD CONSTRAINT agent_profile_versions_profile_id_fkey FOREIGN KEY (profile_id) REFERENCES public.agent_profiles(profile_id) ON DELETE CASCADE;

--
-- Name: agent_profiles agent_profiles_model_light_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_profiles
    ADD CONSTRAINT agent_profiles_model_light_id_fkey FOREIGN KEY (model_light_id) REFERENCES public.agent_models(model_id);

--
-- Name: agent_profiles agent_profiles_model_main_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_profiles
    ADD CONSTRAINT agent_profiles_model_main_id_fkey FOREIGN KEY (model_main_id) REFERENCES public.agent_models(model_id);

--
-- Name: agent_profiles agent_profiles_parent_profile_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_profiles
    ADD CONSTRAINT agent_profiles_parent_profile_id_fkey FOREIGN KEY (parent_profile_id) REFERENCES public.agent_profiles(profile_id) ON DELETE CASCADE;

--
-- Name: agent_profiles agent_profiles_served_version_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_profiles
    ADD CONSTRAINT agent_profiles_served_version_id_fkey FOREIGN KEY (served_version_id) REFERENCES public.agent_profile_versions(version_id) ON DELETE SET NULL;

--
-- Name: agent_prompt_versions agent_prompt_versions_prompt_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_prompt_versions
    ADD CONSTRAINT agent_prompt_versions_prompt_id_fkey FOREIGN KEY (prompt_id) REFERENCES public.agent_prompts(prompt_id) ON DELETE CASCADE;

--
-- Name: agent_skill_resources agent_skill_resources_version_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_skill_resources
    ADD CONSTRAINT agent_skill_resources_version_fkey FOREIGN KEY (skill_version_id) REFERENCES public.agent_skill_versions(skill_version_id) ON DELETE CASCADE;

--
-- Name: agent_skill_versions agent_skill_versions_skill_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_skill_versions
    ADD CONSTRAINT agent_skill_versions_skill_id_fkey FOREIGN KEY (skill_id) REFERENCES public.agent_skills(skill_id) ON DELETE CASCADE;

--
-- Name: agent_traces agent_traces_session_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_traces
    ADD CONSTRAINT agent_traces_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.ai_chat_sessions(id) ON DELETE SET NULL;

--
-- Name: ai_chat_messages ai_chat_messages_session_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_chat_messages
    ADD CONSTRAINT ai_chat_messages_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.ai_chat_sessions(id) ON DELETE CASCADE;

--
-- Name: ai_conversation_insights ai_conversation_insights_session_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_conversation_insights
    ADD CONSTRAINT ai_conversation_insights_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.ai_chat_sessions(id) ON DELETE CASCADE;

--
-- Name: ai_kb_embeddings ai_kb_embeddings_page_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_kb_embeddings
    ADD CONSTRAINT ai_kb_embeddings_page_id_fkey FOREIGN KEY (page_id) REFERENCES public.ai_kb_pages(id) ON DELETE CASCADE;

--
-- Name: ai_kb_embeddings ai_kb_embeddings_source_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_kb_embeddings
    ADD CONSTRAINT ai_kb_embeddings_source_id_fkey FOREIGN KEY (source_id) REFERENCES public.agent_data_sources(source_id) ON DELETE CASCADE;

--
-- Name: ai_kb_pages ai_kb_pages_source_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_kb_pages
    ADD CONSTRAINT ai_kb_pages_source_id_fkey FOREIGN KEY (source_id) REFERENCES public.agent_data_sources(source_id) ON DELETE CASCADE;

--
-- Name: ai_background_leases; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.ai_background_leases ENABLE ROW LEVEL SECURITY;

--
-- Name: ai_conversation_insights; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.ai_conversation_insights ENABLE ROW LEVEL SECURITY;

--
-- Name: SCHEMA public; Type: ACL; Schema: -; Owner: -
--

GRANT USAGE ON SCHEMA public TO anon;
GRANT USAGE ON SCHEMA public TO authenticated;
GRANT USAGE ON SCHEMA public TO service_role;

--
-- Name: FUNCTION ai_conversation_insight_candidates(p_idle_minutes integer, p_lookback_days integer, p_limit integer, p_retry_unclassified boolean); Type: ACL; Schema: public; Owner: -
--

REVOKE ALL ON FUNCTION public.ai_conversation_insight_candidates(p_idle_minutes integer, p_lookback_days integer, p_limit integer, p_retry_unclassified boolean) FROM PUBLIC;
GRANT ALL ON FUNCTION public.ai_conversation_insight_candidates(p_idle_minutes integer, p_lookback_days integer, p_limit integer, p_retry_unclassified boolean) TO service_role;

--
-- Name: FUNCTION ai_conversation_insights_rollup(p_hours integer, p_env text, p_kinds text[]); Type: ACL; Schema: public; Owner: -
--

REVOKE ALL ON FUNCTION public.ai_conversation_insights_rollup(p_hours integer, p_env text, p_kinds text[]) FROM PUBLIC;
GRANT ALL ON FUNCTION public.ai_conversation_insights_rollup(p_hours integer, p_env text, p_kinds text[]) TO service_role;

--
-- Name: FUNCTION ai_reserve_voice_seconds(p_business_id uuid, p_seconds integer, p_limit integer); Type: ACL; Schema: public; Owner: -
--

REVOKE ALL ON FUNCTION public.ai_reserve_voice_seconds(p_business_id uuid, p_seconds integer, p_limit integer) FROM PUBLIC;
GRANT ALL ON FUNCTION public.ai_reserve_voice_seconds(p_business_id uuid, p_seconds integer, p_limit integer) TO service_role;

--
-- Name: TABLE admin_jobs; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.admin_jobs TO anon;
GRANT ALL ON TABLE public.admin_jobs TO authenticated;
GRANT ALL ON TABLE public.admin_jobs TO service_role;

--
-- Name: TABLE agent_audit_log; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.agent_audit_log TO anon;
GRANT ALL ON TABLE public.agent_audit_log TO authenticated;
GRANT ALL ON TABLE public.agent_audit_log TO service_role;

--
-- Name: TABLE agent_bakeoffs; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.agent_bakeoffs TO anon;
GRANT ALL ON TABLE public.agent_bakeoffs TO authenticated;
GRANT ALL ON TABLE public.agent_bakeoffs TO service_role;

--
-- Name: TABLE agent_channels; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.agent_channels TO anon;
GRANT ALL ON TABLE public.agent_channels TO authenticated;
GRANT ALL ON TABLE public.agent_channels TO service_role;

--
-- Name: TABLE agent_data_sources; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.agent_data_sources TO anon;
GRANT ALL ON TABLE public.agent_data_sources TO authenticated;
GRANT ALL ON TABLE public.agent_data_sources TO service_role;

--
-- Name: TABLE agent_deployment_targets; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.agent_deployment_targets TO anon;
GRANT ALL ON TABLE public.agent_deployment_targets TO authenticated;
GRANT ALL ON TABLE public.agent_deployment_targets TO service_role;

--
-- Name: TABLE agent_e2e_runs; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.agent_e2e_runs TO anon;
GRANT ALL ON TABLE public.agent_e2e_runs TO authenticated;
GRANT ALL ON TABLE public.agent_e2e_runs TO service_role;

--
-- Name: TABLE agent_env_vars; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.agent_env_vars TO anon;
GRANT ALL ON TABLE public.agent_env_vars TO authenticated;
GRANT ALL ON TABLE public.agent_env_vars TO service_role;

--
-- Name: TABLE agent_eval_runs; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.agent_eval_runs TO anon;
GRANT ALL ON TABLE public.agent_eval_runs TO authenticated;
GRANT ALL ON TABLE public.agent_eval_runs TO service_role;

--
-- Name: TABLE agent_eval_schedules; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.agent_eval_schedules TO anon;
GRANT ALL ON TABLE public.agent_eval_schedules TO authenticated;
GRANT ALL ON TABLE public.agent_eval_schedules TO service_role;

--
-- Name: TABLE agent_eval_suites; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.agent_eval_suites TO anon;
GRANT ALL ON TABLE public.agent_eval_suites TO authenticated;
GRANT ALL ON TABLE public.agent_eval_suites TO service_role;

--
-- Name: TABLE agent_mcp_servers; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.agent_mcp_servers TO anon;
GRANT ALL ON TABLE public.agent_mcp_servers TO authenticated;
GRANT ALL ON TABLE public.agent_mcp_servers TO service_role;

--
-- Name: TABLE agent_mcp_tools; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.agent_mcp_tools TO anon;
GRANT ALL ON TABLE public.agent_mcp_tools TO authenticated;
GRANT ALL ON TABLE public.agent_mcp_tools TO service_role;

--
-- Name: TABLE agent_models; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.agent_models TO anon;
GRANT ALL ON TABLE public.agent_models TO authenticated;
GRANT ALL ON TABLE public.agent_models TO service_role;

--
-- Name: TABLE agent_profile_mcp_servers; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.agent_profile_mcp_servers TO anon;
GRANT ALL ON TABLE public.agent_profile_mcp_servers TO authenticated;
GRANT ALL ON TABLE public.agent_profile_mcp_servers TO service_role;

--
-- Name: TABLE agent_profile_skills; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.agent_profile_skills TO anon;
GRANT ALL ON TABLE public.agent_profile_skills TO authenticated;
GRANT ALL ON TABLE public.agent_profile_skills TO service_role;

--
-- Name: TABLE agent_profile_versions; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.agent_profile_versions TO anon;
GRANT ALL ON TABLE public.agent_profile_versions TO authenticated;
GRANT ALL ON TABLE public.agent_profile_versions TO service_role;

--
-- Name: TABLE agent_profiles; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.agent_profiles TO anon;
GRANT ALL ON TABLE public.agent_profiles TO authenticated;
GRANT ALL ON TABLE public.agent_profiles TO service_role;

--
-- Name: TABLE agent_prompt_versions; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.agent_prompt_versions TO anon;
GRANT ALL ON TABLE public.agent_prompt_versions TO authenticated;
GRANT ALL ON TABLE public.agent_prompt_versions TO service_role;

--
-- Name: TABLE agent_prompts; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.agent_prompts TO anon;
GRANT ALL ON TABLE public.agent_prompts TO authenticated;
GRANT ALL ON TABLE public.agent_prompts TO service_role;

--
-- Name: TABLE agent_skill_resources; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.agent_skill_resources TO anon;
GRANT ALL ON TABLE public.agent_skill_resources TO authenticated;
GRANT ALL ON TABLE public.agent_skill_resources TO service_role;

--
-- Name: TABLE agent_skill_versions; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.agent_skill_versions TO anon;
GRANT ALL ON TABLE public.agent_skill_versions TO authenticated;
GRANT ALL ON TABLE public.agent_skill_versions TO service_role;

--
-- Name: TABLE agent_skills; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.agent_skills TO anon;
GRANT ALL ON TABLE public.agent_skills TO authenticated;
GRANT ALL ON TABLE public.agent_skills TO service_role;

--
-- Name: TABLE agent_test_runs; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.agent_test_runs TO anon;
GRANT ALL ON TABLE public.agent_test_runs TO authenticated;
GRANT ALL ON TABLE public.agent_test_runs TO service_role;

--
-- Name: TABLE agent_tools; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.agent_tools TO anon;
GRANT ALL ON TABLE public.agent_tools TO authenticated;
GRANT ALL ON TABLE public.agent_tools TO service_role;

--
-- Name: TABLE agent_traces; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.agent_traces TO anon;
GRANT ALL ON TABLE public.agent_traces TO authenticated;
GRANT ALL ON TABLE public.agent_traces TO service_role;

--
-- Name: TABLE ai_background_leases; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.ai_background_leases TO anon;
GRANT ALL ON TABLE public.ai_background_leases TO authenticated;
GRANT ALL ON TABLE public.ai_background_leases TO service_role;

--
-- Name: TABLE ai_chat_messages; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.ai_chat_messages TO anon;
GRANT ALL ON TABLE public.ai_chat_messages TO authenticated;
GRANT ALL ON TABLE public.ai_chat_messages TO service_role;

--
-- Name: TABLE ai_chat_sessions; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.ai_chat_sessions TO anon;
GRANT ALL ON TABLE public.ai_chat_sessions TO authenticated;
GRANT ALL ON TABLE public.ai_chat_sessions TO service_role;

--
-- Name: TABLE ai_conversation_insights; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.ai_conversation_insights TO anon;
GRANT ALL ON TABLE public.ai_conversation_insights TO authenticated;
GRANT ALL ON TABLE public.ai_conversation_insights TO service_role;

--
-- Name: TABLE ai_kb_embeddings; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.ai_kb_embeddings TO anon;
GRANT ALL ON TABLE public.ai_kb_embeddings TO authenticated;
GRANT ALL ON TABLE public.ai_kb_embeddings TO service_role;

--
-- Name: TABLE ai_kb_pages; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.ai_kb_pages TO anon;
GRANT ALL ON TABLE public.ai_kb_pages TO authenticated;
GRANT ALL ON TABLE public.ai_kb_pages TO service_role;

--
-- Name: TABLE ai_user_memory; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.ai_user_memory TO anon;
GRANT ALL ON TABLE public.ai_user_memory TO authenticated;
GRANT ALL ON TABLE public.ai_user_memory TO service_role;

--
-- Name: TABLE ai_voice_budgets; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.ai_voice_budgets TO anon;
GRANT ALL ON TABLE public.ai_voice_budgets TO authenticated;
GRANT ALL ON TABLE public.ai_voice_budgets TO service_role;

--
-- Name: TABLE ai_voice_recording_chunks; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.ai_voice_recording_chunks TO anon;
GRANT ALL ON TABLE public.ai_voice_recording_chunks TO authenticated;
GRANT ALL ON TABLE public.ai_voice_recording_chunks TO service_role;

--
-- Name: TABLE ai_voice_recordings; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.ai_voice_recordings TO anon;
GRANT ALL ON TABLE public.ai_voice_recordings TO authenticated;
GRANT ALL ON TABLE public.ai_voice_recordings TO service_role;

--
-- Name: TABLE ai_voice_scenario_runs; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.ai_voice_scenario_runs TO anon;
GRANT ALL ON TABLE public.ai_voice_scenario_runs TO authenticated;
GRANT ALL ON TABLE public.ai_voice_scenario_runs TO service_role;

--
-- Name: TABLE ai_voice_scenarios; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.ai_voice_scenarios TO anon;
GRANT ALL ON TABLE public.ai_voice_scenarios TO authenticated;
GRANT ALL ON TABLE public.ai_voice_scenarios TO service_role;

--
-- Name: TABLE ai_voice_usage_daily; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.ai_voice_usage_daily TO anon;
GRANT ALL ON TABLE public.ai_voice_usage_daily TO authenticated;
GRANT ALL ON TABLE public.ai_voice_usage_daily TO service_role;

--
-- Name: TABLE test_fixtures; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.test_fixtures TO anon;
GRANT ALL ON TABLE public.test_fixtures TO authenticated;
GRANT ALL ON TABLE public.test_fixtures TO service_role;

--
-- Name: DEFAULT PRIVILEGES FOR SEQUENCES; Type: DEFAULT ACL; Schema: public; Owner: -
--

ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT ALL ON SEQUENCES  TO anon;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT ALL ON SEQUENCES  TO authenticated;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT ALL ON SEQUENCES  TO service_role;

--
-- Name: DEFAULT PRIVILEGES FOR FUNCTIONS; Type: DEFAULT ACL; Schema: public; Owner: -
--

ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT ALL ON FUNCTIONS  TO anon;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT ALL ON FUNCTIONS  TO authenticated;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT ALL ON FUNCTIONS  TO service_role;

--
-- Name: DEFAULT PRIVILEGES FOR TABLES; Type: DEFAULT ACL; Schema: public; Owner: -
--

ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT ALL ON TABLES  TO anon;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT ALL ON TABLES  TO authenticated;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT ALL ON TABLES  TO service_role;


--
-- PostgreSQL database dump complete
--

-- Rows the engine expects to exist.
INSERT INTO public.ai_background_leases VALUES ('conversation_insights', NULL, '1970-01-01 00:00:00+00', NULL, NULL) ON CONFLICT DO NOTHING;
