#!/usr/bin/env bash
# Refresh the vendored LiteLLM pricing snapshot.
#
# Downloads LiteLLM's `model_prices_and_context_window_backup.json`,
# filters it to the v1 providers + LLM-style modes, sorts every key
# for deterministic output, and writes
# `src/llm_usage/core/pricing_data/prices.json`.
#
# Run weekly by `.github/workflows/refresh-pricing.yml`, and runnable
# by hand. Idempotent: re-running with no upstream change leaves the
# file byte-identical, so `git diff` stays empty.
#
# Requires `curl` and `jq` (both preinstalled on GitHub's ubuntu
# runners; `brew install jq` locally on macOS if missing).

set -euo pipefail

LITELLM_URL="https://raw.githubusercontent.com/BerriAI/litellm/main/litellm/model_prices_and_context_window_backup.json"

# Resolve the repo root from this script's location so the script
# works regardless of the caller's working directory.
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="${REPO_ROOT}/src/llm_usage/core/pricing_data/prices.json"

tmp_upstream="$(mktemp)"
tmp_filtered="$(mktemp)"
trap 'rm -f "${tmp_upstream}" "${tmp_filtered}"' EXIT

echo "Downloading LiteLLM pricing JSON ..."
curl -fsSL --max-time 60 -o "${tmp_upstream}" "${LITELLM_URL}"

# `jq -S` sorts the keys of every object recursively: the top-level
# model names AND each entry's fields. That makes the committed file
# fully deterministic, so a weekly-refresh diff only ever shows real
# price / model changes — never an ordering reshuffle if LiteLLM
# happens to reorder its upstream JSON.
#
# Filter clauses (kept in sync with pricing_data/README.md):
#   - litellm_provider is one of the v1 providers
#   - mode is chat or responses (token-priced LLM calls)
#   - the entry actually carries rates (a couple of metadata-only
#     entries have neither input_cost_per_token nor tiered_pricing)
echo "Filtering to v1 providers + chat/responses modes ..."
jq -S '
  to_entries
  | map(select(
      (.value.litellm_provider == "anthropic" or
       .value.litellm_provider == "openai" or
       .value.litellm_provider == "deepseek" or
       .value.litellm_provider == "dashscope")
      and (.value.mode == "chat" or .value.mode == "responses")
      and (.value | has("input_cost_per_token") or has("tiered_pricing"))
    ))
  | from_entries
' "${tmp_upstream}" > "${tmp_filtered}"

count="$(jq 'length' "${tmp_filtered}")"
if [[ "${count}" -eq 0 ]]; then
  echo "ERROR: filtered result is empty — LiteLLM's schema may have changed." >&2
  echo "Refusing to overwrite ${DEST} with an empty pricing table." >&2
  exit 1
fi

mv "${tmp_filtered}" "${DEST}"
echo "Wrote ${count} model entries to ${DEST}"
