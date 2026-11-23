# Create and revoke API keys

An API key lets a program use your agents without anyone signing in. Use one to put an agent behind your own website or app, to run it from a script, or to connect an AI assistant to the portal.

A key is a long piece of text that starts with `wae_`. Whoever holds it can do what the key allows, so treat it like a password.

## Open the screen

Click **Settings** at the bottom of the sidebar, then **API keys**. If you don't see **API keys**, your portal doesn't offer them.

## Create a key

1. Under **Create a key**, type a **Name** that says what will use it, for example "Website chat widget". The name only helps you tell keys apart later.
2. Choose the **Access**:
   - **Chat only**: the key can talk to your agents. It can't change anything. Use this for websites, apps and scripts.
   - **Full access**: the key can also manage the portal from an AI assistant: agents, instructions, tools, your team and other keys. Give this only to people you would make an admin.
3. Choose when it **Expires**. A key that expires stops working on its own on that day. Pick **Never** only for a key you will look after.
4. Click **Create key**.

The new key appears at the top of the page.

5. Click **Copy** and paste the key where it will be used straight away.

**You see the key once.** It isn't saved anywhere, so nobody can show it to you again, including your portal administrator. If you lose it, revoke it and create a new one.

## Use a key

The program sends the key with each request, in a header:

```
Authorization: Bearer wae_...
```

For example, to send a message to the agent whose address is `helpdesk`:

```
curl -N https://your-portal-address/v1/agents/helpdesk/chat \
  -H "Authorization: Bearer wae_..." \
  -H "Content-Type: application/json" \
  -d '{"message": "What are your opening hours?"}'
```

To connect an AI assistant, see [Manage agents from an AI assistant](../09-reference/03-ai-assistant.md). It needs a **Full access** key.

## Check your keys

**Keys** lists every key with:

- **Name** and **Key**: the name you gave it and the first few characters of the key, so you can match it to the one in your program.
- **Access**: **Chat**, or **Chat** and **Admin** for a full-access key.
- **Created** and **Last used**: **Never** means nothing has used the key yet. Last used can be up to a minute behind.
- **Status**: **Active**, **Expired** or **Revoked**.

## Revoke a key

Revoke a key when you no longer need it, when the person who had it leaves, or as soon as you think someone else may have seen it.

1. Find the key in **Keys**.
2. Click **Revoke** and confirm.

The key stops working straight away. Anything still using it is refused, so put a new key in place first if the program must keep running. A revoked key can't be turned back on. It stays in the list so you can see what it was.

## Good to know

- Each key keeps its own conversations. One key can't read or continue another key's chats.
- Don't put a key in a web page or a mobile app where visitors could read it. Keep it on your server.
- Don't paste a key into a chat or an email. If that happens, revoke it.
- Creating and revoking keys is recorded in [Activity](02-activity.md).

**Related:** [Choose who can use the portal](01-team.md) · [See who changed what](02-activity.md) · [Manage agents from an AI assistant](../09-reference/03-ai-assistant.md)
