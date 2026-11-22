# Voice checks

A voice check is a scripted phone call to a voice agent. A simulated caller says set lines, and the check confirms the agent replied and did the right thing, for example introduced itself or booked a technician visit.

![Voice checks, with New voice check, a demo check for Rami and its Run button numbered](../media/testing/voice-checks.png)

On **Testing**, open the **Voice checks** tab.

## Run a check

1. **New voice check** adds a check (see below).
2. Find the check under **Checks**. Each one shows the agent type, the language and how many steps it has.
3. Click **Run**. The result appears under **Recent runs** with **Passed** or **Failed** and how many steps passed, for example **2 / 2**. A failed run names the step that failed.

You can usually leave **Run against another server** as it is.

## Write a check

1. Click **New voice check**.
2. Give it a **Title** that says what should happen, for example "Demo — Rami says who he is and why he is calling".
3. Pick the **Agent type** and the **Language**.
4. **Steps** list, in order, what the caller says and what the agent's reply should contain. They are written in a technical format: copy the steps of a similar check, or ask your portal administrator to write them with you. Keep each check to two to four steps.
5. Leave **On** ticked, click **Save voice check**, then run it once to make sure it passes.

## Voice checks or test suites?

- A **voice check** answers "does the call work?": the agent answers, hears the caller and does what was asked.
- A **test suite** answers "are the answers good?": each answer is graded and you get a score. Voice agents can have test suites too. See [Run a test and read the result](02-run-a-test.md).

## Good to know

- Checks place real calls through the voice service and use some of your daily voice allowance. See [Daily voice allowance](../08-team-and-settings/03-voice-budgets.md).
- If every check fails at the first step, the voice service may be unavailable. Try again later or tell your portal administrator.

**Related:** [Voice settings](../02-agents/05-voice.md) · [Review a conversation](../05-monitor/02-review-a-conversation.md) · [FAQ](../09-reference/02-faq.md)
