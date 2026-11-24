# Set the daily voice allowance

A voice budget caps how many minutes of voice calls an agent can use each day, so costs can't run away. Every agent gets a default allowance. You can give one agent more or fewer minutes.

![The Voice budgets screen with the ID, Daily limit and Note fields and the Save budget button numbered](../media/team-and-settings/voice-budgets.png)

## How it works

- Each agent's voice calls are charged to its own **budget ID**. You find it on the agent's **Voice** tab, under **Daily call budget**.
- The allowance belongs to that ID, not to a voice model. It covers all the calls charged to it, whichever voice model they run on.
- An ID without its own budget gets the default. The top of the page shows how much that is, for example **60.0 minutes** (3600 seconds) a day.
- **Budgets** lists every ID with its own allowance, with the agent's name beside it. If none has one yet, the page says every agent uses the default.
- A budget applies as soon as you save it.

Where your portal serves businesses (accounts that each have their own agents), the page counts minutes per **business** instead: the field is called **Business ID**, and a business without its own budget gets the default.

## Set a budget

Click **Settings** at the bottom of the sidebar, then **Voice budgets**.

1. In **Budget ID**, paste the ID from the agent's **Voice** tab (or, where the page asks for a **Business ID**, the business's ID; ask your portal administrator if you don't know it).
2. In **Daily limit (seconds)**, enter the allowance in seconds. Multiply minutes by 60: 30 minutes is 1800, 60 minutes is 3600, 2 hours is 7200.
3. In **Note**, write why, for example "Pilot, extra minutes until June". This is optional, but it helps your team later.
4. Click **Save budget**.

The ID now appears under **Budgets**. To change its allowance, set a budget for the same ID again.

## Check how much is being used

On the **Overview**, the **Voice calls** section shows minutes used in the period and **Today vs daily allowance**. See [Read the Overview](../05-monitor/01-overview.md). **Using the default today** on this page lists the agents that made calls today without a budget of their own.

## Good to know

- When an agent reaches its allowance, new calls are refused until the next day.
- If usage can't be checked for a moment, calls are allowed. The budget controls cost, not whether calls work.
- Each change is recorded in [Activity](02-activity.md).

**Related:** [Choose who can use the portal](01-team.md) · [Voice settings](../02-agents/05-voice.md) · [Read the Overview](../05-monitor/01-overview.md)
