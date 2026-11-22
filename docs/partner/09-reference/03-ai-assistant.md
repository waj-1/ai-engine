# Manage agents from an AI assistant

You can connect an AI assistant, such as Claude, to the portal and ask it to do things for you in plain words: "Make Lina's greeting shorter and test it", or "Which agent had the most problems this week?". It works with the same agents and settings you see in the portal.

## What you can ask it to do

- **Create**: start a new agent from a template or copy an existing one. Add knowledge bases and documents, skills, tools and test suites.
- **Change**: read and edit an agent's instructions, model, tools, knowledge, sub-agents, voice and memory settings. Save a version, compare two versions or restore an earlier one.
- **Organise**: make categories such as Support or Sales, put agents in them, and list the agents in one. See [Group your agents into categories](../02-agents/09-categories.md).
- **Talk to an agent**: send it test messages and read the replies, as you would in the test chat.
- **Test**: run a test suite, a model comparison or a voice check, then read the result. It can compare several runs and tell you which cases were fixed, which broke and which still fail. It can also suggest better instructions based on the cases that failed.
- **Monitor**: summarise how your agents are doing, find conversations, read a conversation or list what needs attention.
- **Check history**: see who changed what in Activity.

A few examples to try:

- "List my agents and say which ones are drafts."
- "Make a Support category and put Lina and Adam in it."
- "Show me the conversations from yesterday where Lina handed over to a person."
- "Run Adam's test suite and tell me which cases failed."
- "Compare the last two runs of Salma's tests."

## Connect it

1. **Get the connection details.** You need the connection address, which your portal administrator can give you, and a key. If your portal has **Settings › API keys**, [create a key](../08-team-and-settings/04-api-keys.md) with **Full access** and copy it. Otherwise ask your portal administrator for a personal sign-in token.
2. **Add the connection in your AI assistant.** In its settings, add a new connection (it may be called a "connector", "integration" or "MCP server"). Paste the address, and paste the key or token where it asks for one.
3. **Check it works.** Ask "List my agents". You should see the same agents as in the portal.

Your key or token works like your password. Don't share it or paste it into a chat. If you think someone else has a key, revoke it under **Settings › API keys**. For a token, tell your portal administrator straight away so they can turn it off.

## Safety

- **The assistant acts as you.** It can do what you can do in the portal, and nothing more. Every change it makes is recorded in [Activity](../08-team-and-settings/02-activity.md) under your name, or under the key's name if you connected with an API key.
- **Risky changes need a second yes.** Deleting something, changing who has access, or sending a change to live first comes back with a preview of what *would* happen. Nothing changes until you confirm.
- **Read before you confirm.** Check the preview names the right agent and the right change.
- **Test after every change**, in the portal or by asking the assistant to run the tests. See [Test your agent](../02-agents/08-test-your-agent.md).

**Related:** [Make your changes live](../07-publishing/01-publish-changes.md) · [See who changed what](../08-team-and-settings/02-activity.md) · [Glossary](01-glossary.md)
