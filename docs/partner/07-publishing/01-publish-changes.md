# Make your changes live

How a saved change reaches people depends on how your portal is set up. Look at the top right of the portal, next to the search box:

- **You see a STAGING / PRODUCTION switch.** Your install keeps two copies of every agent. You edit the **Staging** copy, test it, then **promote** it to **Production**. See [Portals with Staging and Production](#portals-with-staging-and-production).
- **There is no switch.** Your install serves one copy of each agent, and a change goes live as soon as you save it. See [Portals without the switch](#portals-without-the-switch).

The way to make a change safely, and to undo one, is the same in both.

## Portals with Staging and Production

The switch at the top chooses which copy of your agents you're looking at. A red **Production** banner shows above the page while you work on Production, so you always know which one you're changing. The portal remembers **Production** only until you close the browser. When you open it again you're back on **Staging**.

- **Staging** is where you make changes. A save on Staging applies straight away to everything that uses Staging, including the **Test** chat. An install started from the standard setup serves Staging to its API callers too, so on that install a saved change reaches them on their next message.
- **Production** serves the version you last **promoted** to it, not your saved edits. A new agent shows **Not live yet** in Production until you promote it for the first time. Later edits on Staging don't reach Production until you promote again. Production is only used by an engine that is set to serve production (your administrator sets this when the engine is installed).

### Promote a change to Production

1. Make and test the change on **Staging** (see [A safe way to make a change](#a-safe-way-to-make-a-change)).
2. Open **Settings › Releases › Promote agent changes**. Each agent shows its **Production version**, its **Unpublished changes** and its **Last quality check**. Tick **Only with changes** to see only the agents that differ.
3. Click **Promote** on the agent. A window shows **What will change** in Production (instructions, tools, knowledge, model and so on) and the last test result.
4. Click **Publish to production**. If the last test scored below the bar, or the instructions name a tool the agent can't use, the window asks **Why publish anyway?** first. Your reason is recorded in **Activity**.

You can also promote one agent from its page: **More ▾ → Promote to Production…**. On the agent's **Versions** tab, **Compare with Production** shows how Staging and Production differ.

**Settings › Releases** is only shown to portal administrators. If you can't see it, ask your administrator to promote the change.

### See what was promoted

**Settings › Releases › Promotion history** lists every promote, newest first: the agent, the version it published and the one it replaced, where it came from, who promoted it, when, and what changed. **Live now** marks the version Production serves. (**Deploy history** on the same strip is a different thing: it lists updates to the engine's own code, not agent changes.)

## Portals without the switch

**A change goes live as soon as you save it.** There's no separate publish step: people get the new setup on their next message, or their next call for voice settings. There is no **Promote** button and no **Settings › Releases** page on these portals.

<video src="../media/publishing/save-and-check.mp4" controls muted playsinline width="100%"></video>
*Lina gets a new sign-off line: edit the instructions, save, then check the very next test reply.*

## A safe way to make a change

![Lina's agent page with the Test button, the Versions tab and the More menu highlighted](../media/publishing/more-menu.png)

1. **Save a checkpoint first.** On the **Versions** tab (2), write a note such as "Before new refund rules" and click **Save as new version**. This is your way back.
2. **Make the change** on the right tab and click its Save button: **Save instructions**, **Save tools & knowledge**, **Save sub-agents**, **Save voice settings** or **Save settings**. Write a short **What changed?** note where there's a box for it. With the switch, do this on **Staging**.
3. **Test straight away.** Click **Test** (1) and try the conversation you changed, plus one or two that should still work as before. See [Test your agent](../02-agents/08-test-your-agent.md).
4. **For bigger changes, run a test suite** and compare the result with the last run. See [Run a test](../06-testing/02-run-a-test.md) and [Compare results](../06-testing/03-compare-results.md). With the switch, Production uses the last result when you promote.
5. **With the switch, promote** the agent (see above). The **More ▾** menu (3) has **Promote to Production…**.
6. **Keep an eye on real conversations** for the next few hours. See [Review a conversation](../05-monitor/02-review-a-conversation.md).

## Undo a change

- **Instructions:** on the **Instructions** tab, use **Compare versions** to see the earlier text, put it back in the editor, and click **Save instructions**.
- **Anything else, or several tabs at once:** on the **Versions** tab, click **Restore** on the checkpoint you saved before the change. Then check the tabs and test again.
- **Need time to fix it?** **Pin** your last good checkpoint on the **Versions** tab. People get that version while you work. Choose **Default (latest)** again when you're done. See [Save a checkpoint and go back to it](../02-agents/06-versions.md).
- **With the switch:** undo the change on **Staging** as above, test it, and promote again. Production keeps serving what you last promoted until you do.

## Hold changes back

- A **Draft** agent isn't served to anyone. New agents start as drafts. Switch one to **Active — serves people** in **Settings** when it's ready.
- Pinning a version (see above) keeps people on a known-good setup while you edit.
- With the switch, Production doesn't change until you promote, so you can work on Staging for as long as you need.

## Who changed what

Every save and every promote is recorded with who made it and when. Open **Activity** in the sidebar, or **More ▾ → View activity log** on an agent. See [Activity](../08-team-and-settings/02-activity.md).

**Related:** [Change what your agent says](../02-agents/02-instructions.md) · [Test your agent](../02-agents/08-test-your-agent.md) · [FAQ: my change isn't showing](../09-reference/02-faq.md)
