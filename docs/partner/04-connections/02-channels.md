# Channels: where customers reach your agents

A channel is a place your agents answer from: an app, a website chat widget, a partner portal or an API. Each channel says which agent answers, which websites may use it, and whether people must sign in.

![The Channels list, with New channel and the Answered by column numbered](../media/connections/channels-list.png)

In the sidebar, click **Connections**, then the **Channels** tab. (1) **New channel** adds one. For each channel you see its type, (2) the agent it is **Answered by**, whether **Sign-in** is required, how many websites may use it, and its status. **Chosen per request** means the app picks the agent each time.

## Add a channel

![The New channel form for a website chat answered by Lina](../media/connections/new-channel.png)

1. Click **New channel**, type a **Name** (for example "Demo website chat") and a **Short code name** (lowercase, for example `demo_website_chat`). Your developers use the short code name to say which channel a request comes from.
2. Pick the **Type**, for example **Website widget** for a chat bubble on your website.
3. Under **Answered by**, pick the agent that answers, for example Lina. Under **Agents allowed on this channel**, tick only the agents that may be used here (each agent you made yourself has its own box); leave all unticked to allow every agent.
4. Under **Allowed websites**, add each website address that will show the chat, one per line, like `https://www.example.com` (no slash at the end). Leave it empty only for system-to-system use.
5. Check **Require sign-in** (on for apps where people log in, off for a public website), **Requests per minute** (60 suits most sites) and **Active**, then click **Create channel**.

You can usually leave **Advanced** as it is.

## After creating it

- Give the short code name to your developers; they connect the website or app to it.
- A new allowed website works straight away, with no restart. Where the engine runs on several servers, the others pick it up within about a minute. If the chat still doesn't load on a new website, ask your portal administrator.
- To pause a channel, open it and untick **Active**.

**Related:** [Connect tools and integrations](01-tools-and-integrations.md) · [Your agents](../02-agents/01-your-agents.md) · [Review a conversation](../05-monitor/02-review-a-conversation.md)
