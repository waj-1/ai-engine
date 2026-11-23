# Publishing: change an agent safely, then ship it

[![Watch the video](media/engine-publishing.webp)](media/engine-publishing.mp4)

▶ **Full video with sound:** [engine-publishing.mp4](media/engine-publishing.mp4) (37 s)

Change an agent, check the result straight away, and keep a way back. When your portal has a **STAGING / PRODUCTION** switch in the header, you edit in staging, test there, and promote the agent to production when you are happy. Every release is saved as a version you can pin or restore.

**Where:** Agents › your agent, and Settings › Releases · **Guides:** [Make your changes live](01-publish-changes.md), [Save a checkpoint and go back to it](../02-agents/06-versions.md)

## How it works

### 1. Edit in staging
With **STAGING** selected, open the agent's **Instructions** tab, make your change, write a short **What changed?** note and click **Save instructions**. Here the IT helpdesk gets a sign-off line.

![Instructions tab in staging with the new sign-off line and a What changed note](media/1-edit.jpg)

### 2. Test it straight away
Click **Test** and try the conversation you changed. The answer now ends with the new sign-off.

![Test chat in staging, the answer ends with Anything else I can help with?](media/2-test.jpg)

### 3. See what will change, then promote
**Settings › Releases › Promote agent changes** lists each agent's staging setup next to production, with its unpublished changes and last quality check. Click **Promote** to see what will change (here: **Instructions**, v2 to v3), then **Publish to production**.

![Promote IT helpdesk to production dialog with What will change and Quality check](media/3-promote.jpg)

### 4. Production has it now
Switch to **PRODUCTION**: the agent answers with the new line straight away.

![Test chat in production, the answer ends with the new sign-off](media/4-live.jpg)

### 5. Undo with versions
Each promotion is saved as a version ("Promoted from staging by ..."). On the **Versions** tab, **Pin** a version to serve it, or **Restore** it to copy it back into the editable setup. You are asked to confirm first.

![Versions tab in production with v1 and v2, What changed, Pin and Restore](media/5-versions.jpg)

## Good to know
- Save a checkpoint before a big change: on the **Versions** tab, type a note and click **Save as new version**.
- The promote dialog also shows the agent's last quality check, so you can run a test suite first.
- A pinned version is served whatever you edit afterwards. Choose **Default (latest)** again when you are done.
- Without the STAGING / PRODUCTION switch, a change goes live as soon as you save it. See [Make your changes live](01-publish-changes.md).
- Every save and every promotion is recorded in **Activity**.

## In this section
- [Make your changes live](01-publish-changes.md): a safe way to make a change, test it, and undo it.
