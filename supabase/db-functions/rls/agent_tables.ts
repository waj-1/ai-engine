import { PrismaClient } from '@prisma/client';

const TABLES = [
  'agent_profiles', 'agent_profile_versions', 'agent_models', 'agent_tools',
  'agent_prompts', 'agent_prompt_versions', 'agent_data_sources', 'agent_mcp_servers',
  'agent_channels', 'agent_env_vars', 'agent_deployment_targets', 'agent_traces',
  'agent_e2e_runs', 'agent_eval_suites', 'agent_eval_runs', 'agent_eval_schedules',
  'agent_bakeoffs',
  'agent_test_runs', 'agent_audit_log', 'admin_jobs', 'test_fixtures',
  'ai_chat_sessions', 'ai_chat_messages',
  'ai_kb_pages', 'ai_kb_embeddings', 'ai_voice_usage_daily',
  'ai_voice_budgets', 'ai_voice_scenarios', 'ai_voice_scenario_runs',
  'ai_voice_recordings', 'ai_voice_recording_chunks',
  'ai_conversation_insights', 'ai_background_leases',
  'agent_skills', 'agent_skill_versions', 'agent_skill_resources', 'agent_profile_skills',
];

async function execute(client: PrismaClient) {
  for (const t of TABLES) {
    // language=PostgreSQL
    await client.$executeRawUnsafe(`ALTER TABLE "public"."${t}" ENABLE ROW LEVEL SECURITY;`);
    await client.$executeRawUnsafe(`DROP POLICY IF EXISTS "${t}_service_role_all" ON "public"."${t}";`);
    await client.$executeRawUnsafe(`
      CREATE POLICY "${t}_service_role_all" ON "public"."${t}"
        AS PERMISSIVE FOR ALL TO service_role
        USING (true) WITH CHECK (true);
    `);
  }
}

export default execute;
