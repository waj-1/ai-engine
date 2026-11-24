---
name: compare-models
description: Compare models for an agent on the user's WAJ AI Engine by running the same test suite on each and reporting pass rate, speed and tokens side by side. Use when the user asks which model to use, whether a cheaper, faster or local model is good enough, or wants a model bake-off.
---

# Compare models for an agent

Work through the `waj-ai-engine` MCP server. Each tool describes its own
parameters; read the description before the first call.

## Steps

1. **Find the agent and its suite.** `list_profiles`, `eval_suites`. With no
   suite there is nothing to compare on: use the `test-agent` skill first.
2. **List the candidates.** `models(action="list")`. Compare the agent's
   current model with the ones the user named. If they named none, pick at
   most two alternatives and say why (cheaper, faster, or local).
3. **Run the same suite on each.** `trigger_eval_run` once per model with
   `model_id`, leaving everything else the same, or use `bakeoffs` to run
   them as one comparison. Use repeats of 3 when the suite has fewer than 15
   cases, so one lucky answer does not decide it.
4. **Wait and collect.** Poll `eval_runs` until every run finishes.
5. **Report one table:** model, pass rate, cases that failed only on that
   model, median response time, and tokens used. Then one recommendation in a
   sentence, and what the user gives up by taking it.
6. **Change the agent's model only on a yes**, with `update_profile`.

## Rules

- Say how many model calls the comparison will make before starting.
- A model that fails a refusal or safety case is not "almost as good". Call
  it out separately from the pass rate.
- If a model cannot be reached or has no key, report that; do not drop it
  from the table silently.
