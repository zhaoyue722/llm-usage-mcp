"""Layer 1: automatic capture of LLM API calls.

Phase 1 ships the HTTP-proxy path (Path A in `docs/spec.md`):
`create_proxy_app()` builds a FastAPI app the user points their coding
agent at, and `run_proxy()` is the CLI entry that boots uvicorn on the
loopback interface. The MCP server (Layer 3) reads the events the proxy
writes; the two layers don't talk to each other directly — both write
through `core.recording.record_event()` and read through SQLite.

Subsequent phases extend the same FastAPI app: streaming support and
OpenAI-compatible routes for OpenAI, DeepSeek, and Qwen.
"""

from llm_usage.capture.proxy import create_proxy_app, run_proxy

__all__ = ["create_proxy_app", "run_proxy"]
