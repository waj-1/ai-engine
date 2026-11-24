---
name: test-agent
description: Write a test suite for an agent on the user's WAJ AI Engine and run it - realistic questions with the words, tool calls or rubric each answer must satisfy - then report what passed and failed. Use when the user asks to test an agent, write test cases or evals, check an agent before publishing, or find out whether an agent works.
---

# Test an agent

Work through the `waj-ai-engine` MCP server. Each tool describes its own
parameters; read the description before the first call.

## Steps

1. **Find the agent.** `list_profiles`, then `get_profile` for its
   instructions, model and tools. If several match the user's words, ask which.
2. **Reuse before you write.** `eval_suites(action="list")`. If the agent has a
   suite, show its cases and ask whether to run it, extend it or start again.
3. **Write the cases.** Aim for 8 to 15, each one thing a real person would
   ask. Cover:
   - the main jobs from the instructions, one case each;
   - each attached tool, with a question that should make the agent call it;
   - at least two things the agent must refuse or hand off;
   - one vague message and one message in another language if the agent
     serves more than one.

   For every case say how it is checked: words the answer must contain, a
   tool that must be called, or a rubric for a judge model. Prefer the first
   two; they need no judge. A rubric case needs an evaluator model on the
   suite. If none is set up, say so; do not leave rubric cases to fail
   silently.
4. **Show the cases to the user** as a short table before saving, unless they
   said to go ahead.
5. **Save and run.** `eval_suites(action="create", ...)`, then
   `trigger_eval_run`. It returns a run id at once; poll
   `eval_runs(action="get", ...)` until the run finishes.
6. **Report.** Pass rate, then each failing case with the question, what was
   expected and what the agent said. Sort failures into: the agent is wrong,
   the case is wrong, or a tool or model failed. Fix a wrong case yourself and
   say so. For a wrong agent, offer the `improve-instructions` skill.

## Rules

- A suite runs real model calls. Say roughly how many before running more
  than 30 cases or any repeats.
- Never weaken a case just to make it pass.
- Do not run voice suites in live mode unless the user asks; live calls are
  billed by the provider.
