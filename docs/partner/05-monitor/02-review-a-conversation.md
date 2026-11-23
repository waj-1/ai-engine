# Review a conversation

Read exactly what a customer and your agent said, listen to the call, and see where something went wrong.

<video src="../media/monitor/review-a-conversation.mp4" controls muted playsinline width="100%"></video>
*Filter to Lina's conversations that need attention, open one, jump to the failed step, then listen to the call.*

## Find the conversation

![The Conversations screen with the filters, a selected conversation, the problem box and the tabs numbered](../media/monitor/conversation.png)

1. Click **Conversations** in the sidebar. Narrow the list with the filters at the top:
   - **Period**: from **Last hour** to **Last 30 days**.
   - **Agent** and **Channel**: one agent, or one place customers reach you. Each agent you made yourself is listed by its own name.
   - **Outcome**: **Needs attention**, **Handed to a person** or **No problems**.
   - **Search**: words, a customer's name or a reference. Click **Search**.

   **More filters** has extra options. **Export CSV** downloads the list as a spreadsheet.
2. Click a conversation in the list. Each row shows the agent, when it started, the customer's first message, how many replies the agent gave and a coloured outcome label.

You can also get here from the Overview: **Open latest →** next to a problem, or **Conversations →** next to an agent.

## Read it

3. If something went wrong, a red box at the top explains it: which action failed, why, and what the agent said next. Click **Jump to message** to scroll to that moment.

   Below it, the conversation reads like a chat: the customer's messages on the right, the agent's on the left. Small labels between messages show each action the agent took, with ✓ when it worked and ✕ when it failed.
4. Use the tabs to switch views:
   - **Conversation**: the messages.
   - **Call audio**: for calls, the recording. The caller is on the left channel and the agent on the right.
   - **Technical**: step-by-step timing and details for engineers. You can usually leave it alone.

## Spot what went wrong

Ask yourself:

- **Did an action fail?** Look for ✕ labels. "Could not be reached" or "did not respond in time" means a system was down or slow. Tell your portal administrator if it repeats.
- **Did the agent misunderstand?** Compare what the customer asked with what the agent did. You can often fix this in the [instructions](../02-agents/02-instructions.md).
- **Did it hand over too early or too late?** Check where **Handed to a person** happened.
- **Did a call sound wrong?** Listen on **Call audio**. Long silences or the caller talking over the agent are worth reporting.

## Share it

Click **Copy link** to copy a link to this conversation. Anyone on your team who can sign in can open it. It's also the fastest way to show a problem to your portal administrator.

Then turn the problem into a test case, so you'll know when it's fixed. See [Test suites](../06-testing/06-test-suites.md).

**Related:** [Read the Overview](01-overview.md) · [Test your agent](../02-agents/08-test-your-agent.md) · [FAQ](../09-reference/02-faq.md)

## If tracing is off

Some installs run without the tracing stack. A grey line above the list says so. You still see every conversation and can open and read it, because conversations are always saved.

What changes:

- **More filters** is not shown. Period, Agent, Channel, Outcome and Search still work, and Search looks inside the messages.
- An action label shows the action's name without ✓ or ✕. Whether it worked, and what it sent and received, is recorded only with tracing on.
- **Technical** shows totals for the conversation (tokens, cost, response time) and no step-by-step timing.

Ask your portal administrator to turn tracing on if you need the step-by-step view.
