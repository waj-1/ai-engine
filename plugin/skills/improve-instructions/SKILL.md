---
name: improve-instructions
description: Change an agent's instructions on the user's WAJ AI Engine and prove the change with its test suite before it goes live - draft a new version, run the suite against the draft, compare with the current version case by case, and apply it only if it is better. Use when the user says an agent answers badly, is too long, misses a tool, breaks a rule, or asks to improve, tune or rewrite a prompt or instructions.
---

# Improve an agent's instructions

Work through the `waj-ai-engine` MCP server. Each tool describes its own
parameters; read the description before the first call.

## Steps

1. **Pin down the complaint.** One sentence: what the agent does, and what it
   should do instead. If the user gave an example conversation, find it with
   `search` or `traces` and read it.
2. **Read the current state.** `get_profile` and
   `prompts(action="list_versions", ...)` for the instructions in use.
3. **Make sure there is a test for it.** Find the agent's suite with
   `eval_suites`. If there is none, use the `test-agent` skill first. Add a
   case that fails today because of the complaint; a fix with no failing case
   cannot be proven.
4. **Get a baseline.** `trigger_eval_run` on the current instructions and wait
   for it with `eval_runs`.
5. **Draft the change.** `prompts(action="create_version", ...)`. Change as
   little as you can: a rule added, a rule made specific, a contradiction
   removed. Do not rewrite the whole thing. Creating a version does not put it
   in use.
6. **Test the draft.** `trigger_eval_run` with `prompt_overrides` naming the
   new version number, so the suite runs against the draft while everyone
   else still gets the current one.
7. **Compare case by case.** Which cases the draft fixed, and which it broke.
   A draft that fixes the complaint and breaks something else is not done:
   go back to step 5. Stop after three rounds and report what you learned.
8. **Apply only on a yes.** Show the user the wording change and the two
   results side by side. Put the new version in use only when they agree.

## Rules

- One change per round, so you know what caused the difference.
- If the two runs differ by a single case, run each again with repeats before
  calling it better; small models are not consistent.
- Never edit a test case to make the draft pass.
