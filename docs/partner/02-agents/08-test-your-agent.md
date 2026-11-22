# Test your agent

The test chat lets you talk to an agent from inside the portal, exactly as a customer would, and see its answer straight away. Test after every change you save.

<video src="../media/agents/test-your-agent.mp4" controls muted playsinline width="100%"></video>
*Testing Lina: open the test chat, describe a problem, give the account details, and read her answer.*

## Start a test chat

1. Open the agent and click **Test** at the top right. You can also use the round **Test agent** button at the bottom right of any tab.
2. If a **business** list appears, leave it on **— select a business —** unless the agent needs a business to look things up for. The demo agents don't.
3. Type a message the way a customer would, for example "Hi, my Wi-Fi keeps freezing during the match tonight", and click **Send**.
4. The answer appears as it's written. **Done** under the box means the agent has finished.
5. Carry on the conversation to test a full flow. For example, give the account number and date of birth when Lina asks for them, and check that she verifies you before sharing anything.
6. Close the chat with **×**.

The test chat talks to the agent as it's set up right now: the instructions, tools and knowledge you last saved. If a version is pinned on the **Versions** tab, you're talking to the pinned version instead. See [Versions](06-versions.md).

## What to check

- **Does it follow the rules?** For example, it verifies the customer before sharing account details, and never makes up an amount.
- **Does it use the right action?** If it should check for an outage, does the answer show it did?
- **Is the tone right?** Length, friendliness, and the language the customer wrote in.
- **Does it hand off when it should?** If the agent has sub-agents, ask a question meant for the specialist.

Try a few messages that go wrong on purpose, too: wrong account details, a question the agent shouldn't answer, or a switch to Arabic halfway through.

## Good to know

- Test chats use the same tools as real conversations. The demo agents only have simulated tools, but a real agent's actions may really happen. Use test accounts.
- Your test only covers what you type. To check an agent against many conversations at once and get a score, run a test suite. See [Run a test](../06-testing/02-run-a-test.md).
- To read a real customer conversation in detail, see [Review a conversation](../05-monitor/02-review-a-conversation.md).

**Related:** [Change what your agent says](02-instructions.md) · [Make your changes live](../07-publishing/01-publish-changes.md) · [FAQ](../09-reference/02-faq.md)
