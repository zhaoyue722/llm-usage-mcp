# Model quality data

`model_quality.json` is a vendored, **hand-authored** table of relative
quality scores per `(provider, model)`. It is the quality counterpart
to `pricing_data/prices.json` — kept a separate file (and materialized
into a separate `quality_snapshot` table) because quality and pricing
have independent sources and refresh cadences.

## Schema

A nested object: provider → model → score.

```jsonc
{
  "anthropic": {
    "claude-opus-4-7": 96,     // normalized 0-100, higher is better
    "claude-sonnet-4-6": 91
  },
  "openai": {
    "gpt-5.5-pro": 96
  }
}
```

- **`quality_score`** is a normalized float in `[0, 100]`. The loader
  rejects out-of-range values.
- Model keys **must match the model names in `pricing_snapshot`** (i.e.
  the names produced by `pricing_loader` after its provider-rename and
  prefix-strip normalization). `recommend_provider` joins the two
  tables on `(provider, model)`; a mismatch silently drops the model
  from recommendations.

## Status: placeholder data

The scores here are **hand-authored editorial estimates**, not measured
benchmarks. They encode a rough, plausible ordering — flagship models
in the mid-90s, mid-tier ~82-89, budget ~73-78 — enough for
`recommend_provider` to have meaningful tiers to choose between. They
are **not** authoritative and should not be cited as benchmark results.

Only a curated subset of each provider's models is scored — the
notable current-generation models, not every dated snapshot or variant
in the pricing table. A model absent from this file simply has no
quality signal; `recommend_provider` treats it accordingly.

## Refresh procedure

Manual for now. The post-v1 roadmap has a leaderboard importer (e.g.
pulling from a public LLM leaderboard) that would **overwrite this file
only** — pricing data is untouched. That importer would emit the same
nested `provider → model → score` shape, normalizing whatever the
leaderboard publishes (Elo, win-rate, etc.) onto the 0-100 scale.

Until then, edit `model_quality.json` by hand and re-run the server;
`bootstrap()` materializes it into `quality_snapshot` on a fresh
database (it does not overwrite an already-populated table).
