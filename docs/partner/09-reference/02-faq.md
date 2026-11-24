# Questions people often ask

Quick answers to the problems that come up most. Each one points to the guide with the full steps.

## My change isn't showing

- **Did you save?** Each tab has its own Save button, such as **Save instructions**. While **Unsaved changes** shows at the bottom of the page, your change isn't saved yet.
- **Start a new conversation.** A change reaches people on their **next** message, or their next call for voice settings. A test chat that was already open may still be on the old setup. Close it and click **Test** again.
- **Is it on Production?** If your portal has a STAGING / PRODUCTION switch, Production serves the version you last promoted. Edit on Staging, then promote the agent in **Settings › Releases** (or **More ▾ → Promote to Production…**). A new agent is **Not live yet** in Production until its first promote.
- **Is a version pinned?** If someone pinned an earlier version on the **Versions** tab, people keep getting that one. Choose **Default (latest)** again.
- **Did you change the right agent?** Check the name at the top of the page. A **Draft** copy doesn't serve anyone.

See [Make your changes live](../07-publishing/01-publish-changes.md).

## The agent says it can't do something

- **Does it have the action?** Open the agent's **Tools & knowledge** tab and check the action is chosen. See [Tools and knowledge](../02-agents/03-tools-and-knowledge.md).
- **Does it have the information?** If it should answer from a document, check the right knowledge base is attached and the document is in it. See [Knowledge bases](../03-knowledge/01-knowledge-bases.md).
- **Do the instructions forbid it?** Read the **Instructions** tab for a rule that says not to. See [Change what your agent says](../02-agents/02-instructions.md).

## A test failed

1. Open the run and read the failed cases first. Each one says what was expected and what the agent did.
2. Decide: is the agent wrong, or is the expected answer out of date?
3. Fix the instructions or the case, then run the suite again and compare the two runs.

See [Run a test](../06-testing/02-run-a-test.md) and [Compare results](../06-testing/03-compare-results.md).

## A call dropped or sounded wrong

1. Find the call in **Conversations** and listen on **Call audio**. See [Review a conversation](../05-monitor/02-review-a-conversation.md).
2. Check the **Overview** for **Today vs daily allowance**. When a business uses up its voice minutes, new calls are refused. See [Set the daily voice allowance](../08-team-and-settings/03-voice-budgets.md).
3. If the agent talks over callers or waits too long, look at the turn-taking settings. See [Voice settings](../02-agents/05-voice.md).
4. Still wrong? Click **Copy link** on the conversation and send it to your portal administrator.

## An action keeps failing

If **Needs attention** shows "could not be reached" or "did not respond in time" again and again, the system behind that action is down or slow. That's not something you can fix in the agent. Send your portal administrator a link to one of the conversations.

## The Overview shows zero, or "Unavailable right now"

Check the period first: **Last 24 hours** may simply be quiet. **Unavailable right now** means the numbers couldn't be loaded, not that there were none. Try again in a few minutes.

## Someone can't sign in

They need to be an **Admin** on **Team & access**. See [Choose who can use the portal](../08-team-and-settings/01-team.md).

**Related:** [See who changed what](../08-team-and-settings/02-activity.md) · [Glossary](01-glossary.md) · [Find anything](../01-getting-started/03-find-anything.md)
