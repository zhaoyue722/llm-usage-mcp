"""Tests for `llm_usage.config`.

Covers defaults, env-var overrides for project-namespaced fields and the
SDK-standard API-key aliases, `.env` loading, `enabled_providers` parsing,
and the `Settings.require_keys()` refuse-to-start gate.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from llm_usage.config import (
    KNOWN_PROVIDERS,
    ConfigurationError,
    Settings,
    get_settings,
)
from llm_usage.core.db.session import resolve_db_url


def test_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "LLM_USAGE_DB_URL",
        "LLM_USAGE_LOG_LEVEL",
        "LLM_USAGE_LOG_PROMPTS",
        "LLM_USAGE_PROXY_PORT",
        "LLM_USAGE_ENABLED_PROVIDERS",
        "LLM_USAGE_ANTHROPIC_BASE_URL",
        "LLM_USAGE_OPENAI_BASE_URL",
        "LLM_USAGE_QWEN_BASE_URL",
        "LLM_USAGE_DEEPSEEK_BASE_URL",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "DASHSCOPE_API_KEY",
        "DEEPSEEK_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)

    s = Settings()
    assert s.db_url.startswith("sqlite:///") and s.db_url.endswith(".llm-usage/usage.db")
    assert s.log_level == "INFO"
    assert s.log_prompts is False
    assert s.proxy_port == 5525
    assert s.enabled_providers == KNOWN_PROVIDERS
    assert s.anthropic_base_url == "https://api.anthropic.com"
    assert s.openai_base_url == "https://api.openai.com/v1"
    assert s.qwen_base_url == "https://dashscope.aliyuncs.com/compatible-mode/v1"
    assert s.deepseek_base_url == "https://api.deepseek.com"
    assert s.api_key_for("anthropic") is None
    assert s.api_key_for("openai") is None
    assert s.api_key_for("qwen") is None
    assert s.api_key_for("deepseek") is None


def test_env_overrides_project_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_USAGE_DB_URL", "sqlite:///custom.db")
    monkeypatch.setenv("LLM_USAGE_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("LLM_USAGE_LOG_PROMPTS", "1")
    monkeypatch.setenv("LLM_USAGE_PROXY_PORT", "9999")
    monkeypatch.setenv("LLM_USAGE_ANTHROPIC_BASE_URL", "https://anthropic.test")
    monkeypatch.setenv("LLM_USAGE_OPENAI_BASE_URL", "https://openai.test")
    monkeypatch.setenv("LLM_USAGE_QWEN_BASE_URL", "https://qwen.test")
    monkeypatch.setenv("LLM_USAGE_DEEPSEEK_BASE_URL", "https://deepseek.test")

    s = Settings()
    assert s.db_url == "sqlite:///custom.db"
    assert s.log_level == "DEBUG"
    assert s.log_prompts is True
    assert s.proxy_port == 9999
    assert s.anthropic_base_url == "https://anthropic.test"
    assert s.openai_base_url == "https://openai.test"
    assert s.qwen_base_url == "https://qwen.test"
    assert s.deepseek_base_url == "https://deepseek.test"


def test_api_keys_read_from_sdk_standard_names(monkeypatch: pytest.MonkeyPatch) -> None:
    """Key fields read from the SDK-standard names, not `LLM_USAGE_*`."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-test")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-dashscope-test")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek-test")

    s = Settings()
    assert s.api_key_for("anthropic") == SecretStr("sk-ant-test")
    assert s.api_key_for("openai") == SecretStr("sk-openai-test")
    assert s.api_key_for("qwen") == SecretStr("sk-dashscope-test")
    assert s.api_key_for("deepseek") == SecretStr("sk-deepseek-test")


def test_secret_str_does_not_leak_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-supersecret")
    s = Settings()
    assert "sk-ant-supersecret" not in repr(s)
    assert "sk-ant-supersecret" not in str(s)
    key = s.api_key_for("anthropic")
    assert key is not None
    assert key.get_secret_value() == "sk-ant-supersecret"


def test_enabled_providers_csv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_USAGE_ENABLED_PROVIDERS", "anthropic, openai")
    s = Settings()
    assert s.enabled_providers == frozenset({"anthropic", "openai"})


def test_enabled_providers_rejects_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_USAGE_ENABLED_PROVIDERS", "anthropic,gemini")
    with pytest.raises(ValidationError) as exc_info:
        Settings()
    assert "gemini" in str(exc_info.value)


def test_enabled_providers_empty_string_yields_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_USAGE_ENABLED_PROVIDERS", "")
    s = Settings()
    assert s.enabled_providers == frozenset()


def test_dotenv_file_loaded(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`Settings` reads a `.env` file from cwd when the field isn't already in env."""
    monkeypatch.delenv("LLM_USAGE_DB_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    (tmp_path / ".env").write_text(
        "LLM_USAGE_DB_URL=sqlite:///from-dotenv.db\nOPENAI_API_KEY=sk-from-dotenv\n"
    )
    monkeypatch.chdir(tmp_path)

    s = Settings()
    assert s.db_url == "sqlite:///from-dotenv.db"
    key = s.api_key_for("openai")
    assert key is not None
    assert key.get_secret_value() == "sk-from-dotenv"


def test_env_overrides_dotenv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Process env beats `.env` (pydantic-settings default precedence)."""
    (tmp_path / ".env").write_text("LLM_USAGE_DB_URL=sqlite:///from-dotenv.db\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LLM_USAGE_DB_URL", "sqlite:///from-process-env.db")

    s = Settings()
    assert s.db_url == "sqlite:///from-process-env.db"


def test_require_keys_passes_when_all_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a")
    monkeypatch.setenv("OPENAI_API_KEY", "b")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "c")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "d")
    Settings().require_keys()


def test_require_keys_raises_with_missing_listed(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "DASHSCOPE_API_KEY", "DEEPSEEK_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "set")  # only openai has a key

    with pytest.raises(ConfigurationError) as exc_info:
        Settings().require_keys()
    msg = str(exc_info.value)
    # The error lists every missing provider and the env var name to set.
    assert "anthropic" in msg
    assert "qwen" in msg
    assert "deepseek" in msg
    assert "openai" not in msg.split("missing API keys for required providers:")[1].split(".")[0]
    assert "ANTHROPIC_API_KEY" in msg
    assert "DASHSCOPE_API_KEY" in msg
    assert "DEEPSEEK_API_KEY" in msg


def test_require_keys_only_checks_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """A disabled provider with no key is fine."""
    monkeypatch.setenv("LLM_USAGE_ENABLED_PROVIDERS", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    for var in ("ANTHROPIC_API_KEY", "DASHSCOPE_API_KEY", "DEEPSEEK_API_KEY"):
        monkeypatch.delenv(var, raising=False)

    Settings().require_keys()


def test_require_keys_subset_narrows_the_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """A caller can demand only a specific subset (Phase 1 proxy use case).

    `LLM_USAGE_ENABLED_PROVIDERS` defaults to all four providers, but the
    Anthropic-only capture proxy shouldn't require OpenAI / Qwen / DeepSeek
    keys the user hasn't set yet.
    """
    for var in ("OPENAI_API_KEY", "DASHSCOPE_API_KEY", "DEEPSEEK_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a")

    Settings().require_keys({"anthropic"})  # subset narrows to just Anthropic


def test_require_keys_subset_still_raises_for_missing_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The subset gates whichever providers it names — missing keys still raise."""
    for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "DASHSCOPE_API_KEY", "DEEPSEEK_API_KEY"):
        monkeypatch.delenv(var, raising=False)

    with pytest.raises(ConfigurationError, match="anthropic"):
        Settings().require_keys({"anthropic"})


def test_get_settings_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_USAGE_DB_URL", "sqlite:///first.db")
    first = get_settings()
    assert first.db_url == "sqlite:///first.db"

    # Mutating env without clearing cache leaves the singleton stale.
    monkeypatch.setenv("LLM_USAGE_DB_URL", "sqlite:///second.db")
    assert get_settings() is first

    # cache_clear() picks up the new value.
    get_settings.cache_clear()
    assert get_settings().db_url == "sqlite:///second.db"


def test_resolve_db_url_still_honors_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: `resolve_db_url()` reflects `LLM_USAGE_DB_URL` via Settings."""
    monkeypatch.setenv("LLM_USAGE_DB_URL", "sqlite:///wired-through-settings.db")
    assert resolve_db_url() == "sqlite:///wired-through-settings.db"


def test_proxy_port_out_of_range_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_USAGE_PROXY_PORT", "70000")
    with pytest.raises(ValidationError):
        Settings()


def test_log_level_invalid_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_USAGE_LOG_LEVEL", "VERBOSE")
    with pytest.raises(ValidationError):
        Settings()


def test_base_url_for_every_provider() -> None:
    s = Settings()
    assert s.base_url_for("anthropic") == "https://api.anthropic.com"
    assert s.base_url_for("openai") == "https://api.openai.com/v1"
    assert s.base_url_for("qwen") == "https://dashscope.aliyuncs.com/compatible-mode/v1"
    assert s.base_url_for("deepseek") == "https://api.deepseek.com"
