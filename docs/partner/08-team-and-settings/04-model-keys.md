# Add a model key

A model key is what lets your agents use an AI provider such as Google Gemini, OpenAI, Anthropic or Azure OpenAI. You get it from the provider's own website. Paste it here once and every model of that provider uses it.

## How it works

- Each provider has one key. The **Providers** table shows, for each, which key is in use: **Stored key** (saved on this page), **Environment variable** (set on the server by whoever installed it) or **No key**.
- A key saved here is used first. If you remove it, the provider goes back to the server's own key, if it has one.
- Keys are stored encrypted. After you save one it is never shown again: you only see its last four characters, so you can tell keys apart.
- A new key is used from the agent's next message.
- On a portal with a STAGING / PRODUCTION switch, Staging and Production each have their own stored keys. The page keeps the switch and names the one you're looking at (**Staging** or **Production**) above the table. Save the key on both if both need it.

## Save a key

Click **Settings** at the bottom of the sidebar, then **Model keys**.

1. Find the provider's row.
2. Paste the key into the box under **Set or replace**.
3. Click **Save**. If the provider already had a stored key, the button says **Replace** and the new key takes its place.
4. Click **Test**. The portal asks the provider for its list of models using the key. It costs nothing and sends no message. A tick means the provider accepted the key. A cross comes with the provider's own explanation, for example that the key is invalid or has expired.

## Remove a key

Click **Remove** in the provider's row and confirm. If the server has its own key for that provider it is used instead; otherwise models of that provider stop answering until you save a new key.

## If the page says keys cannot be stored

The server needs a setting called `SECRET_KEY` to encrypt keys. Without it the page shows a warning, the boxes are disabled and nothing is saved. Ask whoever runs the server to set `SECRET_KEY` and restart it. An install started with `docker compose up` already has one.

## A second key for the same provider

Most teams never need this. If two models of one provider must use different keys, for example two OpenAI-compatible servers, save the extra key under **Named keys** with a short name such as `catalog`. Then open the model under **Agents › Models** and add `"credential": "catalog"` to its extra settings. Models without that setting keep using the provider's main key.

## Good to know

- When you add a model, the **New model** form tells you whether the provider you picked has a key.
- **Cannot be read** next to a stored key means the server's `SECRET_KEY` changed since the key was saved. Paste the key again.
- Every save and removal is recorded in [Activity](02-activity.md), without the key.

**Related:** [Choose who can use the portal](01-team.md) · [See who changed what](02-activity.md) · [Create a new agent](../02-agents/07-new-agent.md)
