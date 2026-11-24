# The public demo and Build with AI

Some portals are a **public demo** of the engine: a yellow **Public demo** bar sits at the top of every page. In a public demo:

- You get in with one click. On the sign-in page, click **Try the demo**. There is no account to make and no password to type. You land on **Agents**, in staging, where the demo's agents are built and tested.
- **Anyone can see what you type.** Everyone shares the same demo, so don't enter anything private: no real names, emails, passwords or card numbers.
- **Everything resets every night.** Agents, documents, tests and conversations you made are deleted, and the sample agents come back as they were.
- Some screens are switched off: model keys, API keys, environment variables, team and access, diagnostics, developer docs and code deploys. The link in the bar shows how to run your own copy, where every screen works.
- There is a daily limit. When the demo has used it, agents and the assistant answer that the demo has reached today's limit. Try again the next day, or run your own copy.

The demo's agents work for **Northwind Internet**, a made-up home internet provider:

| Agent | What it does |
|---|---|
| Support desk | Answers customers. Uses Northwind's tools (outages, orders, tickets, plans) and the **Northwind help center** knowledge base, and hands billing questions to the Billing helper. |
| Billing helper | A specialist the Support desk asks about bills, prices and refunds. |
| Sales assistant | Helps people choose a plan. |
| IT helpdesk | Answers Northwind staff's everyday IT questions. |
| Builder assistant | The assistant in the **Build with AI** panel (below). It is read-only and not in the Agents list, so every visitor gets the same one. |

## Build with AI

The **Build with AI** panel opens on the right the first time you visit. Close it with **×** and bring it back with the **Build with AI** button at the bottom right.

Tell it what you want in plain words and it does the work for you with the engine's own tools, the same ones the portal uses. Each step it takes shows as a small line in its answer, with a link to what it made or changed. For example:

- "Build me a support agent that checks outages": it creates the agent from a template, writes its instructions and gives it Northwind's tools and help center.
- "Test the Support desk": it runs the Support desk's test suite, waits for it (up to two minutes) and tells you the score and any case that failed, with a link to the run.
- "Add a refund specialist": it creates a specialist and makes it a sub-agent of the Support desk.
- "Why did this test fail?": it reads the run and suggests one fix.
- "Chat with the Sales assistant as a family of four": it sends test messages and tells you what the agent answered.

Each step shows what it did in plain words ("Created Outage helper from a template", "Attached the Northwind tools", "Started a test run"), with a link where there is something to open. When it has changed something, click **Refresh this page** in its answer to see the change on the screen you have open, or follow a link. The conversation stays in the panel as you move between pages, even if you follow a link while it is still working. The suggestions stay as a small row under the conversation. **New** starts a new conversation. **About this demo**, at the bottom of the panel, says what the assistant is.

What it cannot do in the demo: change model keys, API keys, users, settings or deployments, or connect a tool server other than the built-in Northwind one. It also takes a limited number of steps per message and per hour: if it stops part-way, ask it to carry on.

The Builder assistant is an ordinary agent. Open it on **Agents** to read its instructions and see which model it uses.
