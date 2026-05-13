"""Process-wide configuration: env vars, `.env`, and the refuse-to-start gate.

A single `Settings(BaseSettings)` owns every knob the rest of the codebase
reads — DB URL, log level/prompts flag, proxy port, per-provider base URLs,
per-provider API keys, and which providers are enabled. Everything is
sourced from environment variables (with a `.env` fallback) and validated
at construction.

Conventions:

- **`LLM_USAGE_` prefix** for project-owned knobs (`LLM_USAGE_DB_URL`,
  `LLM_USAGE_PROXY_PORT`, `LLM_USAGE_ANTHROPIC_BASE_URL`, ...). Matches
  the existing `LLM_USAGE_DB_URL` used by `core.db.session`.
- **Upstream-standard names for API keys**: `ANTHROPIC_API_KEY`,
  `OPENAI_API_KEY`, `DASHSCOPE_API_KEY` (Qwen), `DEEPSEEK_API_KEY` —
  these are the names every provider's SDK and docs already use, and
  most users already have them set.
- **`SecretStr` for keys** so they don't leak into logs / reprs by
  accident. Call `.get_secret_value()` when you actually need the
  string (at the request boundary).

Public API:

- `get_settings()` — process-wide cached `Settings`. Tests reset with
  `get_settings.cache_clear()`.
- `Settings.api_key_for(provider)` / `Settings.base_url_for(provider)`.
- `Settings.require_keys()` — raises `ConfigurationError` listing every
  *enabled* provider whose key is missing. Consumers (proxy, SDK
  wrappers) call this at their own startup; it is never invoked
  implicitly here, so importing this module is side-effect-free.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Final, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

Provider = Literal["anthropic", "openai", "qwen", "deepseek"]

KNOWN_PROVIDERS: Final[frozenset[Provider]] = frozenset({"anthropic", "openai", "qwen", "deepseek"})

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

_DEFAULT_DB_PATH: Final[Path] = Path.home() / ".llm-usage" / "usage.db"


class ConfigurationError(RuntimeError):
    """Raised when required configuration is missing or invalid.

    Distinct from `ValueError` so callers can catch "this is a config
    problem, tell the user to fix their env" without also catching
    every other bad-input case.
    """


class Settings(BaseSettings):
    """All process-wide configuration, sourced from env / `.env`.

    Field names use snake_case; pydantic-settings looks them up under
    `LLM_USAGE_<UPPER>` by default. The four API-key fields override
    the prefix via `validation_alias` so they read from the
    SDK-standard names (`ANTHROPIC_API_KEY`, etc.).
    """

    model_config = SettingsConfigDict(
        env_prefix="LLM_USAGE_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- general --------------------------------------------------------

    db_url: str = Field(default=f"sqlite:///{_DEFAULT_DB_PATH}")
    log_level: LogLevel = "INFO"
    log_prompts: bool = False
    proxy_port: int = Field(default=8787, ge=1, le=65535)

    # `NoDecode` tells pydantic-settings not to JSON-decode the env value
    # before handing it to our validator — we accept a CSV string.
    enabled_providers: Annotated[frozenset[Provider], NoDecode] = Field(default=KNOWN_PROVIDERS)

    # --- per-provider base URLs (project-namespaced env vars) -----------

    anthropic_base_url: str = "https://api.anthropic.com"
    openai_base_url: str = "https://api.openai.com/v1"
    qwen_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    deepseek_base_url: str = "https://api.deepseek.com"

    # --- per-provider API keys (SDK-standard env var names) -------------

    anthropic_api_key: SecretStr | None = Field(default=None, validation_alias="ANTHROPIC_API_KEY")
    openai_api_key: SecretStr | None = Field(default=None, validation_alias="OPENAI_API_KEY")
    # Qwen's API key is read from DASHSCOPE_API_KEY (the upstream name).
    # We surface the provider as "qwen" but the env var stays DashScope-flavored.
    dashscope_api_key: SecretStr | None = Field(default=None, validation_alias="DASHSCOPE_API_KEY")
    deepseek_api_key: SecretStr | None = Field(default=None, validation_alias="DEEPSEEK_API_KEY")

    # --- validators -----------------------------------------------------

    @field_validator("enabled_providers", mode="before")
    @classmethod
    def _parse_enabled_providers(cls, value: object) -> object:
        """Accept a CSV string from env (`anthropic,openai`) or a set/list.

        pydantic-settings hands us whatever was in the env var (a string),
        but constructing `Settings(enabled_providers={"anthropic"})` in
        tests should also work — so the validator handles both.
        """
        if isinstance(value, str):
            names = [item.strip() for item in value.split(",") if item.strip()]
            unknown = sorted(set(names) - KNOWN_PROVIDERS)
            if unknown:
                raise ValueError(
                    f"unknown providers in LLM_USAGE_ENABLED_PROVIDERS: {unknown}; "
                    f"known providers are {sorted(KNOWN_PROVIDERS)}"
                )
            return frozenset(names)
        return value

    # --- accessors ------------------------------------------------------

    def api_key_for(self, provider: Provider) -> SecretStr | None:
        """Return the configured API key for `provider`, or `None`."""
        match provider:
            case "anthropic":
                return self.anthropic_api_key
            case "openai":
                return self.openai_api_key
            case "qwen":
                return self.dashscope_api_key
            case "deepseek":
                return self.deepseek_api_key

    def base_url_for(self, provider: Provider) -> str:
        """Return the base URL for `provider` (default or env-overridden)."""
        match provider:
            case "anthropic":
                return self.anthropic_base_url
            case "openai":
                return self.openai_base_url
            case "qwen":
                return self.qwen_base_url
            case "deepseek":
                return self.deepseek_base_url

    def require_keys(self) -> None:
        """Raise `ConfigurationError` if any enabled provider's key is missing.

        Called explicitly by the proxy / SDK-wrapper startup paths. Not
        invoked anywhere implicitly — the MCP server, the CLI, and pure
        library imports stay usable without provider keys (you can query
        cached spend without an API key in hand).
        """
        missing: list[Provider] = sorted(
            p for p in self.enabled_providers if self.api_key_for(p) is None
        )
        if missing:
            raise ConfigurationError(
                "missing API keys for enabled providers: "
                + ", ".join(missing)
                + ". Set the corresponding env var(s): "
                + ", ".join(_env_var_for(p) for p in missing)
                + ". Or narrow LLM_USAGE_ENABLED_PROVIDERS to exclude them."
            )


def _env_var_for(provider: Provider) -> str:
    """The user-facing env var name for a provider's API key."""
    match provider:
        case "anthropic":
            return "ANTHROPIC_API_KEY"
        case "openai":
            return "OPENAI_API_KEY"
        case "qwen":
            return "DASHSCOPE_API_KEY"
        case "deepseek":
            return "DEEPSEEK_API_KEY"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide `Settings` instance.

    Cached so repeated reads don't reparse env / `.env`. Tests that
    mutate env vars must call `get_settings.cache_clear()` after each
    change.
    """
    return Settings()


__all__ = [
    "KNOWN_PROVIDERS",
    "ConfigurationError",
    "LogLevel",
    "Provider",
    "Settings",
    "get_settings",
]
