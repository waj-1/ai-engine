# Give an agent a skill

A skill is a short set of instructions an agent pulls in only when it needs them, for example "how to explain a refund". Skills keep the agent's main instructions short and let you reuse the same know-how.

![The New skill form filled in for Lina, with the main fields numbered](../media/knowledge/skills-new.png)

## Create a skill

1. In the sidebar, click **Knowledge**, then the **Skills** tab. Type a **Name**, for example "Demo — explain a refund". Fill in the **Short code name** too: lowercase letters, digits, - or _ (for example `explain-refund`).
2. Under **Agent**, pick the agent that can use it, for example **Lina — Customer support**. Leave **Business** blank so it works for every business.
3. Write a **One-line description** (the agent reads it to decide when the skill is useful) and the **Instructions**: when to use the skill and the steps to follow.
4. Leave **Always on** unticked so the agent loads the skill only when it's relevant. Tick it only for rules the agent needs in every conversation.
5. Click **Create skill**. The skill appears in the list above the form.

## Edit a skill

Click a skill in the list to open it. Every time you save new instructions, a new version is kept, so you can see what changed and go back.

## When to use a skill, a knowledge base or instructions

| Use | For |
|---|---|
| **Instructions** | Who the agent is and the rules it always follows. See [Change what your agent says](../02-agents/02-instructions.md). |
| **Skill** | A procedure the agent needs now and then, like handling a refund request. |
| **Knowledge base** | Facts the agent looks up: prices, policies, guides. See [Add documents to a knowledge base](01-knowledge-bases.md). |

## Tips

- Keep each skill to one job, and say in the description exactly when it applies.
- Don't use curly braces in the description.
- Test the agent after adding a skill: ask a question that should trigger it. See [Test your agent](../02-agents/08-test-your-agent.md).

**Related:** [Choose tools and knowledge for an agent](../02-agents/03-tools-and-knowledge.md) · [What agents remember](03-memory.md) · [Glossary](../09-reference/01-glossary.md)
