import { PrismaClient } from '@prisma/client';

const TABLES = [
  'agent_profiles', 'agent_profile_versions', 'agent_models', 'agent_tools',
  'agent_prompts', 'agent_prompt_versions', 'agent_data_sources', 'agent_mcp_servers',
  'agent_channels', 'agent_env_vars', 'agent_deployment_targets', 'agent_traces',
  'agent_e2e_runs', 'agent_eval_suites', 'agent_eval_runs', 'agent_eval_schedules',
  'agent_bakeoffs',
  'agent_test_runs', 'agent_audit_log', 'admin_jobs', 'test_fixtures',
  'ai_chat_sessions', 'ai_chat_messages', 'ai_user_memory', 'ai_voice_usage_daily',
  'ai_public_usage_daily',
  'ai_voice_recordings',
  'agent_skills', 'agent_skill_versions', 'agent_skill_resources',
];

// language=PostgreSQL
const FN = `
CREATE OR REPLACE FUNCTION "public"."set_updated_at"()
RETURNS TRIGGER LANGUAGE plpgsql AS $function$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$function$;
`;

async function execute(client: PrismaClient) {
  await client.$executeRawUnsafe(FN);
  for (const t of TABLES) {
    // Only attach where the table actually has an updated_at column.
    const sql = `
      DO $$
      BEGIN
        IF EXISTS (
          SELECT 1 FROM information_schema.columns
          WHERE table_schema='public' AND table_name='${t}' AND column_name='updated_at'
        ) THEN
          DROP TRIGGER IF EXISTS set_updated_at ON "public"."${t}";
          CREATE TRIGGER set_updated_at BEFORE UPDATE ON "public"."${t}"
            FOR EACH ROW EXECUTE FUNCTION "public"."set_updated_at"();
        END IF;
      END $$;
    `;
    await client.$executeRawUnsafe(sql);
  }
}

export default execute;
