/**
 * Deploys the AI project's db-functions (rls/triggers/validation/rpc) via Prisma
 * raw SQL against AI_DATABASE_URL (loaded as DATABASE_URL by env.ts). Each file
 * default-exports `execute(client: PrismaClient)`. Idempotent: re-run on every deploy.
 *
 * Selective deploy honours CHANGED_FILES (newline-separated repo-relative paths)
 * and DEPLOY_ALL=true, mirroring the root runner.
 */
import { readdirSync, statSync } from 'fs';
import { join } from 'path';

import { PrismaClient } from '@prisma/client';

import env from './env';

type FunctionType = 'rpc' | 'rls' | 'triggers' | 'validation';
const DIRECTORIES: FunctionType[] = ['rpc', 'rls', 'triggers', 'validation'];

const DB_FUNCTIONS_PREFIX = 'supabase/db-functions/';

function validateEnvVariables() {
  if (!env) {
    throw new Error('❌ Environment variables are not loaded');
  }
}

function isDirectory(source: string) {
  return statSync(source).isDirectory();
}

function getFiles(root: string, subDir: string) {
  const dir = join(root, subDir);
  return readdirSync(dir).filter((file) => {
    if (file === '.gitkeep') return false;
    return !isDirectory(join(dir, file));
  });
}

function parseChangedFiles(): Set<string> | null {
  if (process.env.DEPLOY_ALL === 'true') return null;
  const raw = process.env.CHANGED_FILES;
  if (!raw || !raw.trim()) return null;
  const paths = raw
    .split(/\r?\n/)
    .map((l) => l.trim())
    .filter((l) => l.length > 0)
    .filter((p) => p.startsWith(DB_FUNCTIONS_PREFIX));
  return new Set(paths);
}

function shouldDeploy(changed: Set<string> | null, dir: FunctionType, file: string) {
  if (changed === null) return true;
  return changed.has(`${DB_FUNCTIONS_PREFIX}${dir}/${file}`);
}

async function run() {
  validateEnvVariables();
  const prisma = new PrismaClient({ datasources: { db: { url: process.env.DATABASE_URL } } });
  const rootDir = join(__dirname);
  const changed = parseChangedFiles();

  const allErrors: string[] = [];
  for (const key of DIRECTORIES) {
    const files = getFiles(rootDir, key).filter((f) => shouldDeploy(changed, key, f));
    console.info(`ℹ️ Deploying ${key} functions (${files.length})...`);
    for (const [index, file] of Object.entries(files)) {
      const fileName = file.split('.').at(0);
      try {
        console.info(`🚀 (${+index + 1}/${files.length}) Deploying ${fileName}...`);
        const module = await import('file://' + join(rootDir, key, file));
        // Support both ESM (module.default is the function) and CJS interop
        // (module.default is wrapped as { default: fn }) depending on tsx compile mode.
        const fn =
          typeof module.default === 'function'
            ? module.default
            : (module.default as { default: (client: PrismaClient) => Promise<void> })['default'];
        await fn(prisma);
        console.info(`✅ Deployed ${fileName}`);
      } catch (error) {
        console.error(`❌ Error deploying ${fileName}: ${(error as Error).message}`);
        allErrors.push(fileName!);
      }
    }
  }

  await prisma.$disconnect();
  if (allErrors.length > 0) {
    console.error(`❌ Deployment partially failed with ${allErrors.length} errors`);
    return 1;
  }
  console.log('✅ Deployment successful!');
  return 0;
}

run()
  .then((code) => process.exit(code))
  .catch((error) => {
    console.error(`❌ Unexpected error: ${error.message}`);
    process.exit(1);
  });
