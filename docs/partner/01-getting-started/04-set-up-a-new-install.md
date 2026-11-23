# Set up a new install

The first time you open an engine you installed yourself, it has no admin account and no default login. A short setup page creates the account and gets you a model key and an API key. It takes about two minutes.

If someone else installed the portal and gave you a login, skip this page: your portal is already set up, and the setup page no longer exists.

## Before you start

Open the engine's address in your browser (for a local install, `http://localhost:8000`). You are taken to the setup page.

You need the **setup code**. The engine prints it in its log each time it starts, so only someone who can read the server's log can claim a new install:

```bash
docker compose logs engine
```

Look for the block that starts with **FIRST-RUN SETUP**. The code looks like `K7QM-2XHD-9RTV-4NPB`.

## Step 1: create the admin account

1. Type the **Setup code**. Dashes and capitals don't matter.
2. Type your **Email** and a **Password** of at least 10 characters, then the password again. Common and easy-to-guess passwords are refused.
3. Click **Create admin account**.

You are signed in straight away.

## Step 2: add a model key (optional)

Agents need an AI model to answer with.

- To use a provider such as Google Gemini, OpenAI, Anthropic or Azure OpenAI: choose the **Provider**, paste its **Key** and click **Save and test**. The page tells you whether the provider accepted the key. Then click **Continue**.
- To use a model that runs on your own machine: open **I will use a local model**, read the short instructions, and click **Skip for now**.
- To decide later: click **Skip for now**. You can add a key any time under **Settings**, then **Model keys**. See [Add a model key](../08-team-and-settings/04-model-keys.md).

If the page says a key cannot be stored on this server, the server is missing its `SECRET_KEY` setting. Skip the step and ask whoever runs the server to set it.

## Step 3: copy your API key

The last page shows an API key with **Full access**, plus two ready-made examples that already contain it: a command that talks to an agent, and the settings that connect an AI assistant to the portal.

1. Click **Copy** next to the key and keep it somewhere safe, such as a password manager.
2. Click **Go to the portal**.

**You see the key once.** Only a fingerprint of it is stored. If you lose it, revoke it and create another under **Settings**, then **API keys**. See [Create and revoke API keys](../08-team-and-settings/04-api-keys.md).

## If something goes wrong

- **"That setup code is not right."** Check you are reading the newest code: every start of the engine prints a new one, and the older ones stop working.
- **"Setup is locked."** After 5 wrong codes the page locks. Restart the engine (`docker compose restart engine`) and use the new code from its log.
- **"The setup code has expired."** A code lasts 24 hours. Restart the engine for a new one.
- **You are sent to the sign-in page instead.** The admin account already exists, so setup is closed for good. Sign in with that account. If you lost the password, ask whoever runs the server to reset it.

## Installs without the setup page

If the server was started with `ADMIN_EMAIL` and `ADMIN_PASSWORD` set, that account is created automatically and the setup page never appears. Sign in with those details.

**Related:** [Welcome to the portal](01-welcome.md) · [Add a model key](../08-team-and-settings/04-model-keys.md) · [Create and revoke API keys](../08-team-and-settings/04-api-keys.md)
