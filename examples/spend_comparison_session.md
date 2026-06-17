# Example session: "what did I spend, and what would the alternatives cost?"

A real session against `llm-usage-mcp` on 2026-05-23. The agent has the
MCP server wired up; the user is asking spend questions in plain
English. Token counts and cost numbers below are unedited tool output.

## 1. "Give me a usage_summary for today."

The agent calls `usage_summary(period="today")`. Result:

```json
{
  "period": "today",
  "total_cost_usd": 0.0008121,
  "call_count": 122,
  "top_providers": [
    {"provider": "qwen", "cost_usd": 0.0008121, "pct": 100}
  ],
  "top_models": [
    {"model": "qwen-flash", "cost_usd": 0.0008121, "pct": 100}
  ],
  "largest_call": {
    "id": "2a459da1-7798-4d5f-868d-2ada3cbd2f52",
    "model": "qwen-flash",
    "cost_usd": 0.00001765,
    "timestamp": 1779529185860
  }
}
```

Reading it back: 122 calls today, all on `qwen-flash`, total spend
**$0.000812**. Largest single call was ~$0.0000177.

## 2. "Compare providers for the input/output tokens I just spent — what would DeepSeek or Anthropic have cost?"

Two-step. First the agent needs the input/output token counts (the
summary only returns cost), so it calls `query_spend` over today:

```json
{
  "total_cost_usd": 0.0008121,
  "total_calls": 122,
  "total_input_tokens": 4490,
  "total_output_tokens": 1469,
  "groups": [
    {"key": "qwen", "cost_usd": 0.0008121, "calls": 122,
     "input_tokens": 4490, "output_tokens": 1469}
  ]
}
```

Then it projects that workload (4,490 in / 1,469 out) across every
priced model via `compare_providers`. Selected rows from the ranking
(cheapest first):

| Rank | Provider | Model | Cost (USD) | vs. cheapest |
|---|---|---|---|---|
| 1 | qwen | qwen-turbo | $0.000518 | 100% |
| 5 | openai | gpt-5-nano | $0.000812 | 157% |
| 7 | qwen | qwen-flash | $0.000812 | 157% (what we used) |
| 11 | deepseek | deepseek-coder | $0.001040 | 201% |
| 19 | deepseek | deepseek-v3.2 | $0.001845 | 356% |
| 20 | deepseek | deepseek-chat | $0.001874 | 362% |
| 28 | anthropic | claude-3-haiku-20240307 | $0.002959 | 571% |
| 50 | deepseek | deepseek-r1 | $0.005687 | 1097% |
| 69 | anthropic | claude-haiku-4-5 | $0.011835 | 2283% |
| 124 | anthropic | claude-sonnet-4-6 | $0.035505 | 6850% |
| 143 | anthropic | claude-opus-4-7 | $0.059175 | 11417% |
| 165 | anthropic | claude-opus-4-1 | $0.177525 | 34251% |

## Takeaways

- **Same workload on the cheapest DeepSeek** (`deepseek-coder`): $0.00104 — about $0.0002 more than what we paid on qwen-flash.
- **Same workload on Claude Sonnet 4.6**: $0.0355, ~44x more.
- **Same workload on Claude Opus 4.7**: $0.0592, ~73x more.
- `qwen-turbo` would have been ~36% cheaper still at this volume; worth a swap if the quality holds up for the task.

## What this exercises

- `usage_summary` — calendar-UTC window, top-3 rollups, largest call.
- `query_spend` — same window narrower output: token totals broken down by provider.
- `compare_providers` — projects a hypothetical workload across every
  model in the pricing table; cheapest gets `relative_cost_pct = 100`,
  everything else is its multiple. Cost only (no quality axis in v1).

Three tools, one natural question, fully local — no provider
dashboards, no currency conversion done by hand.
