import { config } from 'dotenv-safe';
import { join } from 'path';
import { z } from 'zod';

config({
  example: join(__dirname, '.env.example'),
  path: join(__dirname, '.env'),
  allowEmptyValues: true,
});

const envVariables = z.object({
  DATABASE_URL: z.string(),
});

envVariables.parse(process.env);

declare global {
  namespace NodeJS {
    // eslint-disable-next-line @typescript-eslint/no-empty-object-type
    interface ProcessEnv extends z.infer<typeof envVariables> {}
  }
}
export default envVariables;
