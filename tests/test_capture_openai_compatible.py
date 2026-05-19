"""Unit tests for the OpenAI-compatible capture module's pure helpers.

The integration shape (route → upstream → record_event row landed) is
exercised in `test_capture_openai_compat_routes.py`. Here we pin the
provider-specific token extractors with recorded response fixtures so
the cache-token mappings (OpenAI's nested `prompt_tokens_details`,
DeepSeek's `prompt_cache_hit/miss_tokens` siblings, Qwen's missing-
cache-field default-to-zero) stay correct under refactors.
"""

from __future__ import annotations

import json
from pathlib import Path

from llm_usage.capture.openai_compatible import (
    _extract_deepseek,
    _extract_openai,
    _extract_qwen,
)

_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "sample_responses"


# --- OpenAI ----------------------------------------------------------------


def test_extract_openai_full_payload() -> None:
    """Real-shape fixture with cached_tokens populated."""
    data = json.loads((_FIXTURE_DIR / "openai_chat_completions_ok.json").read_text())
    kwargs = _extract_openai(data)
    assert kwargs is not None
    assert kwargs["provider"] == "openai"
    assert kwargs["model"] == "gpt-5.2"
    # prompt_tokens=20, cached_tokens=8 → input = 20 - 8 = 12 (the uncached portion)
    assert kwargs["input_tokens"] == 12
    assert kwargs["cache_read_tokens"] == 8
    assert kwargs["cache_write_tokens"] == 0  # OpenAI doesn't bill cache writes separately
    assert kwargs["output_tokens"] == 30
    assert kwargs["request_id"] == "chatcmpl-openaitest123"
    assert kwargs["success"] is True


def test_extract_openai_missing_prompt_tokens_details_treats_cached_as_zero() -> None:
    """Older / non-cache-eligible models omit `prompt_tokens_details` entirely."""
    data = {
        "id": "chatcmpl-x",
        "model": "gpt-3.5-turbo",
        "usage": {"prompt_tokens": 14, "completion_tokens": 8, "total_tokens": 22},
    }
    kwargs = _extract_openai(data)
    assert kwargs is not None
    assert kwargs["input_tokens"] == 14
    assert kwargs["cache_read_tokens"] == 0


def test_extract_openai_null_prompt_tokens_details_treats_cached_as_zero() -> None:
    """Some responses send the nested object as null."""
    data = {
        "id": "chatcmpl-x",
        "model": "gpt-x",
        "usage": {
            "prompt_tokens": 5,
            "completion_tokens": 3,
            "prompt_tokens_details": None,
        },
    }
    kwargs = _extract_openai(data)
    assert kwargs is not None
    assert kwargs["input_tokens"] == 5
    assert kwargs["cache_read_tokens"] == 0


def test_extract_openai_returns_none_on_missing_required_fields() -> None:
    """Required fields: `id`, `model`, `usage`. Anything else missing → None."""
    assert _extract_openai({"id": "x", "model": "m"}) is None  # no usage
    assert _extract_openai({"id": "x", "usage": {}}) is None  # no model
    assert _extract_openai({"model": "m", "usage": {}}) is None  # no id
    assert _extract_openai({"id": 1, "model": "m", "usage": {}}) is None  # id not str
    assert _extract_openai({"id": "x", "model": "m", "usage": "no"}) is None  # usage not dict


# --- DeepSeek --------------------------------------------------------------


def test_extract_deepseek_full_payload() -> None:
    """DeepSeek's explicit cache-hit / cache-miss split.

    Real-shape fixture: prompt_cache_hit_tokens=4 and
    prompt_cache_miss_tokens=10. Our schema's input_tokens is the
    "fresh"/miss portion (full-price), cache_read_tokens is the hit
    portion (discounted), cache_write_tokens is always 0 (DeepSeek
    bills writes at the regular input rate the first time, so we
    never see a "creation" token count separately).
    """
    data = json.loads((_FIXTURE_DIR / "deepseek_chat_completions_ok.json").read_text())
    kwargs = _extract_deepseek(data)
    assert kwargs is not None
    assert kwargs["provider"] == "deepseek"
    assert kwargs["model"] == "deepseek-chat"
    assert kwargs["input_tokens"] == 10  # miss
    assert kwargs["cache_read_tokens"] == 4  # hit
    assert kwargs["cache_write_tokens"] == 0
    assert kwargs["output_tokens"] == 25
    assert kwargs["request_id"] == "chatcmpl-deepseektest456"


def test_extract_deepseek_zero_hits_is_all_miss() -> None:
    """First call to a model: every prompt token is a cache miss."""
    data = {
        "id": "chatcmpl-d",
        "model": "deepseek-chat",
        "usage": {
            "prompt_tokens": 14,
            "completion_tokens": 8,
            "prompt_cache_hit_tokens": 0,
            "prompt_cache_miss_tokens": 14,
        },
    }
    kwargs = _extract_deepseek(data)
    assert kwargs is not None
    assert kwargs["input_tokens"] == 14
    assert kwargs["cache_read_tokens"] == 0


def test_extract_deepseek_missing_cache_fields_treats_both_as_zero() -> None:
    """Defensive: if DeepSeek ever omits the split, don't crash — record zeros.

    This would underreport billing, but the alternative (computing
    `input_tokens = prompt_tokens` and `cache_read_tokens = 0`) would
    over-bill if cache was actually hit. Zero-zero is the honest
    "we don't know" default; the warning logged upstream surfaces it.
    """
    data = {
        "id": "chatcmpl-d",
        "model": "deepseek-chat",
        "usage": {"prompt_tokens": 14, "completion_tokens": 8},
    }
    kwargs = _extract_deepseek(data)
    assert kwargs is not None
    assert kwargs["input_tokens"] == 0
    assert kwargs["cache_read_tokens"] == 0


# --- Qwen ------------------------------------------------------------------


def test_extract_qwen_no_cache_fields() -> None:
    """DashScope OpenAI-compatible responses usually omit cache details."""
    data = json.loads((_FIXTURE_DIR / "qwen_chat_completions_ok.json").read_text())
    kwargs = _extract_qwen(data)
    assert kwargs is not None
    assert kwargs["provider"] == "qwen"
    assert kwargs["model"] == "qwen-turbo"
    assert kwargs["input_tokens"] == 18
    assert kwargs["cache_read_tokens"] == 0
    assert kwargs["cache_write_tokens"] == 0
    assert kwargs["output_tokens"] == 12
    assert kwargs["request_id"] == "chatcmpl-qwentest789"


def test_extract_qwen_handles_cached_tokens_if_present() -> None:
    """Qwen's OpenAI-compatible mode *can* populate cached_tokens.

    No real model does today, but the field is in the spec. The
    extractor uses the same OpenAI-shaped reader so this works
    automatically.
    """
    data = {
        "id": "chatcmpl-q",
        "model": "qwen-max",
        "usage": {
            "prompt_tokens": 20,
            "completion_tokens": 5,
            "prompt_tokens_details": {"cached_tokens": 8},
        },
    }
    kwargs = _extract_qwen(data)
    assert kwargs is not None
    assert kwargs["input_tokens"] == 12  # 20 - 8
    assert kwargs["cache_read_tokens"] == 8
