// WAJ AI Engine on Railway, as code. Guide: docs/deploy/railway.md
//
// Railway reads this with its CLI ("Infrastructure as Code"), not during a
// deploy. From a clone of the repository, with the Railway CLI logged in and
// linked to an EMPTY project:
//
//   npm install railway
//   export ENGINE_POSTGRES_PASSWORD="$(openssl rand -hex 24)"
//   export ENGINE_JWT_SECRET="$(openssl rand -hex 32)"
//   export ENGINE_SECRET_KEY="$(openssl rand -hex 32)"
//   railway config plan  --file deploy/railway/railway.ts
//   railway config apply --file deploy/railway/railway.ts
//
// Five services on the project's private network. Only Engine gets a public
// domain (generate it in the dashboard: Engine > Settings > Networking).
//
// No secret is in this file. The three secrets come from the ENGINE_*
// variables of the shell that runs the first apply and are stored on the
// service that owns them. Later applies need no variables: an unset one
// means "keep the value Railway already has".
import { defineRailway, github, image, preserve, project, service, volume } from "railway/iac";

declare const process: { env: Record<string, string | undefined> };

const REPO = "waj-1/ai-engine";
const ENGINE_IMAGE = "ghcr.io/waj-1/ai-engine:latest";

/** A secret from the shell that runs the apply, or the stored value. */
const secret = (name: string) => process.env[name] || preserve();

export default defineRailway(() => {
  const data = volume("engine-pg-data", { sizeMB: 5000 });

  // Postgres with pgvector, built from deploy/paas/db.Dockerfile.
  const db = service("Database", {
    source: github(REPO),
    build: { builder: "DOCKERFILE", dockerfilePath: "deploy/paas/db.Dockerfile" },
    volumeMounts: { "/var/lib/postgresql/data": data },
    deploy: { restartPolicyType: "ALWAYS" },
    env: {
      POSTGRES_PASSWORD: secret("ENGINE_POSTGRES_PASSWORD"),
    },
  });

  // The auth service behind the admin login. It owns the signing secret.
  const auth = service("Auth", {
    source: image("ghcr.io/supabase/gotrue:v2.158.1"),
    // It exits while the database is not up yet, then starts on a retry.
    deploy: { restartPolicyType: "ALWAYS" },
    env: {
      GOTRUE_JWT_SECRET: secret("ENGINE_JWT_SECRET"),
      PGHOST: db.env.RAILWAY_PRIVATE_DOMAIN,
      PGPORT: "5432",
      PGDATABASE: "postgres",
      PGUSER: "postgres",
      PGPASSWORD: db.env.POSTGRES_PASSWORD,
      GOTRUE_DB_DRIVER: "postgres",
      GOTRUE_DB_DATABASE_URL: "postgres:///postgres?search_path=auth",
      GOTRUE_API_HOST: "0.0.0.0",
      GOTRUE_API_PORT: "9999",
      // Nothing outside the private network calls the auth service, and the
      // engine sends no email links, so these two addresses are placeholders.
      API_EXTERNAL_URL: "http://gateway.railway.internal:8000",
      GOTRUE_SITE_URL: "http://engine.railway.internal",
      GOTRUE_URI_ALLOW_LIST: "*",
      GOTRUE_DISABLE_SIGNUP: "true",
      GOTRUE_JWT_ADMIN_ROLES: "service_role",
      GOTRUE_JWT_AUD: "authenticated",
      GOTRUE_JWT_DEFAULT_GROUP_NAME: "authenticated",
      GOTRUE_JWT_EXP: "3600",
      GOTRUE_EXTERNAL_EMAIL_ENABLED: "true",
      GOTRUE_MAILER_AUTOCONFIRM: "true",
    },
  });

  // The REST layer the engine's data client speaks.
  const rest = service("REST", {
    source: image("postgrest/postgrest:v12.2.3"),
    deploy: { restartPolicyType: "ALWAYS" },
    env: {
      PGHOST: db.env.RAILWAY_PRIVATE_DOMAIN,
      PGPORT: "5432",
      PGDATABASE: "postgres",
      PGUSER: "authenticator",
      PGPASSWORD: db.env.POSTGRES_PASSWORD,
      PGRST_DB_URI: "postgresql://",
      PGRST_DB_SCHEMAS: "public",
      PGRST_DB_ANON_ROLE: "anon",
      PGRST_DB_USE_LEGACY_GUCS: "false",
      PGRST_SERVER_HOST: "0.0.0.0",
      PGRST_SERVER_PORT: "3000",
      PGRST_JWT_SECRET: auth.env.GOTRUE_JWT_SECRET,
    },
  });

  // One address in front of REST and Auth, built from
  // deploy/paas/gateway.Dockerfile. Keep it private: it has no key check.
  const gateway = service("Gateway", {
    source: github(REPO),
    build: { builder: "DOCKERFILE", dockerfilePath: "deploy/paas/gateway.Dockerfile" },
    deploy: { restartPolicyType: "ALWAYS" },
    env: {
      REST_HOST: rest.env.RAILWAY_PRIVATE_DOMAIN,
      AUTH_HOST: auth.env.RAILWAY_PRIVATE_DOMAIN,
    },
  });

  // The engine and its admin portal: the only service to give a domain.
  const engine = service("Engine", {
    source: image(ENGINE_IMAGE),
    // Applies the schema, then starts the engine on $PORT.
    start: "python deploy/paas/start_engine.py",
    healthcheck: "/health",
    healthcheckTimeout: 600,
    deploy: { restartPolicyType: "ALWAYS" },
    env: {
      PORT: "8000",
      SECRET_KEY: secret("ENGINE_SECRET_KEY"),
      JWT_SECRET: auth.env.GOTRUE_JWT_SECRET,
      POSTGRES_PASSWORD: db.env.POSTGRES_PASSWORD,
      GATEWAY_HOST: gateway.env.RAILWAY_PRIVATE_DOMAIN,
      GATEWAY_PORT: "8000",
      DB_HOST: db.env.RAILWAY_PRIVATE_DOMAIN,
      DB_PORT: "5432",
      // On the first deploy Database and Gateway are still building when the
      // engine starts. It waits this long for them before giving up.
      INIT_WAIT_SEC: "540",
    },
  });

  return project("waj-ai-engine", {
    resources: [db, auth, rest, gateway, engine, data],
  });
});
