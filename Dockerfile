# Minimal image used for Glama MCP validation.
# This project is intended to run locally via `uvx llm-usage-mcp`;
# the container exists only so automated registries can verify
# that the packaged server boots and responds to MCP introspection.
FROM python:3.13-slim

RUN pip install --no-cache-dir llm-usage-mcp

# Pin the SQLite ledger to a guaranteed-writable path so first-run
# bootstrap (migrate + pricing materialization) succeeds in the
# registry's sandbox regardless of the container's HOME.
ENV LLM_USAGE_DB_URL=sqlite:////tmp/usage.db

ENTRYPOINT ["llm-usage-mcp"]
