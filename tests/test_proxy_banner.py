"""The startup banners (watch-pom + info panel) for proxy and MCP server."""

from __future__ import annotations

from llm_usage.cli_render import format_mcp_banner, format_proxy_banner

_PROVIDERS = [
    ("anthropic", True, "http://127.0.0.1:5525"),
    ("openai", True, "http://127.0.0.1:5525/openai/v1"),
    ("deepseek", False, "http://127.0.0.1:5525/deepseek/v1"),
    ("qwen", False, "http://127.0.0.1:5525/qwen/v1"),
]


def _banner(color_enabled: bool = False) -> str:
    return format_proxy_banner(
        version="0.1.0",
        host="127.0.0.1",
        port=5525,
        db_path="~/.llm-usage/usage.db",
        providers=_PROVIDERS,
        color_enabled=color_enabled,
    )


def test_banner_has_dog_title_and_panel() -> None:
    out = _banner()
    assert "o-''" in out  # the watch-pom is present
    assert "llm-usage-proxy  v0.1.0" in out
    assert "your LLM spend watchdog" in out
    assert "http://127.0.0.1:5525   (loopback only)" in out
    assert "~/.llm-usage/usage.db" in out


def test_banner_shows_each_provider_key_state_and_base_url() -> None:
    out = _banner()
    assert "anthropic" in out and "ready" in out
    assert "no key" in out  # deepseek / qwen unset
    for _, _, base in _PROVIDERS:
        assert base in out


def test_banner_plain_when_color_disabled() -> None:
    assert "\x1b[" not in _banner(color_enabled=False)


def test_banner_colored_when_enabled() -> None:
    assert "\x1b[" in _banner(color_enabled=True)


def test_banner_handles_no_providers() -> None:
    out = format_proxy_banner(
        version="0.1.0",
        host="127.0.0.1",
        port=5525,
        db_path="/tmp/x.db",
        providers=[],
        color_enabled=False,
    )
    assert "llm-usage-proxy" in out  # doesn't crash on an empty provider list


def _mcp(color_enabled: bool = False) -> str:
    return format_mcp_banner(
        version="0.1.0", db_path="~/.llm-usage/usage.db", color_enabled=color_enabled
    )


def test_mcp_banner_has_dog_title_and_transport() -> None:
    out = _mcp()
    assert "o-''" in out  # same watch-pom
    assert "llm-usage-mcp  v0.1.0" in out
    assert "stdio (MCP)" in out
    assert "~/.llm-usage/usage.db" in out


def test_mcp_banner_omits_proxy_only_fields() -> None:
    out = _mcp()
    # No listening URL or client base URLs — those are proxy concepts.
    assert "listening" not in out
    assert "http://127.0.0.1" not in out


def test_mcp_banner_color_toggles_ansi() -> None:
    assert "\x1b[" not in _mcp(color_enabled=False)
    assert "\x1b[" in _mcp(color_enabled=True)
