---
name: build-agent
description: Build a new agent on the user's WAJ AI Engine from a plain-language description - pick a template, set the model, write the instructions, attach tools and knowledge, try it in chat, and publish it when the user says so. Use when the user asks to create, build, set up or scaffold an agent, assistant or chatbot on their engine.
---

# Build an agent

Work through the `waj-ai-engine` MCP server. Each tool describes its own
parameters; read the description before the first call and do not guess
values. The user's explicit instructions override every default below.

## What you need from the user

- **What the agent is for** and who talks to it. Ask once if this is missing.
- **Its name.** The address is made from the name ("Support desk" answers at
  `/v1/agents/support-desk/chat`).

Do not ask about anything else up front. Choose sensible defaults, say what
you chose, and let the user correct you.

## Steps

1. **Look before you build.** `list_profiles` to see whether an agent like
   this already exists. If one does, ask whether to change it or make a new
   one.
2. **Pick a model.** `models(action="list")`. If there is none, stop and tell
   the user to add a model key under Settings › Model keys or a local model
   under Agents › Models. Do not create a model row with a made-up name.
3. **Start from a template.** `templates(action="list")`, choose the closest
   one (Blank when nothing fits), then `templates(action="use", ...)` with the
   name and the model. This makes a draft agent and returns its profile id.
4. **Write the instructions.** `prompts(action="create_version", ...)` for the
   new agent. Write for the agent, in the second person: its job, what it must
   never do, how it should sound, and when it should use each tool. Keep it
   under a page. Name tools by their real names from step 5.
5. **Attach tools.** `mcp_servers(action="list")` shows the connected servers.
   Attach only the actions this agent needs with `profile_mcp_attachments`.
   If the systems the agent needs are not connected, say which server is
   missing; do not invent one.
6. **Attach knowledge** if the user named documents: `kb_sources` to find the
   knowledge base, then attach it to the agent.
7. **Try it.** `chat` with two or three realistic messages, including one the
   agent should refuse or hand off. Read the reply and the tool calls. Fix the
   instructions and try again until it behaves.
8. **Report.** Show the user: the agent's name and address, its model, the
   tools attached, one sample exchange, and anything you could not do.
9. **Publish only when asked.** A new agent is a draft. Make it active with
   `update_profile` only after the user says to publish. Promotion to another
   environment (`promote_profile`) always needs the user's explicit yes.

## Rules

- Never store or print a model key. Keys go through Settings › Model keys.
- Do not delete or archive anything the user did not name.
- If a tool call is refused, show the refusal as it is and stop; do not look
  for another way around it.
- Offer the `test-agent` skill at the end: an agent with no tests cannot be
  changed safely later.
