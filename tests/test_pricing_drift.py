"""Override drift detection.

Overrides in `pricing_overrides.json` win over LiteLLM unconditionally
(field-level merge), so a stale override silently masks correct upstream
prices — exactly what 4x-overcharged deepseek-v4-pro until #49. These
tests are the guardrail: a shipped override must genuinely diverge from
LiteLLM, and the catalog test fails the moment one goes redundant.
"""

from __future__ import annotations

import warnings

from llm_usage.core.pricing_loader import (
    OverrideDrift,
    _detect_drift,
    detect_override_drift,
)


def test_redundant_override_flagged() -> None:
    """Override value equals LiteLLM → redundant, should be removed."""
    base = {"deepseek/x": {"input_cost_per_token": 1e-7, "output_cost_per_token": 2e-7}}
    overrides = {"deepseek/x": {"input_cost_per_token": 1e-7}}
    [finding] = _detect_drift(base, overrides)
    assert finding.kind == "redundant"


def test_diverged_override_reports_delta() -> None:
    """Override value differs from LiteLLM → diverged, with both values in detail."""
    base = {"deepseek/x": {"input_cost_per_token": 1e-7}}
    overrides = {"deepseek/x": {"input_cost_per_token": 5e-7}}
    [finding] = _detect_drift(base, overrides)
    assert finding.kind == "diverged"
    assert "input_cost_per_token" in finding.detail
    assert repr(1e-7) in finding.detail and repr(5e-7) in finding.detail


def test_gap_fill_when_model_absent_from_litellm() -> None:
    """Override key LiteLLM doesn't carry → gap_fill, not drift."""
    overrides = {"deepseek/brand-new": {"input_cost_per_token": 1e-7}}
    [finding] = _detect_drift({}, overrides)
    assert finding.kind == "gap_fill"


def test_underscore_metadata_ignored() -> None:
    """`_reason` / `_added` docs metadata never counts as a price diff."""
    base = {"deepseek/x": {"input_cost_per_token": 1e-7}}
    overrides = {
        "deepseek/x": {
            "input_cost_per_token": 1e-7,
            "_reason": "LiteLLM ships the promo price",
            "_added": "2026-06-23",
        }
    }
    [finding] = _detect_drift(base, overrides)
    assert finding.kind == "redundant"


def test_override_pinning_no_price_field_is_redundant() -> None:
    base = {"deepseek/x": {"input_cost_per_token": 1e-7}}
    overrides = {"deepseek/x": {"mode": "chat", "source": "https://example.com"}}
    [finding] = _detect_drift(base, overrides)
    assert finding.kind == "redundant"


def test_repo_has_no_redundant_overrides() -> None:
    """CI guard against the real files.

    A shipped override must actually diverge from (or gap-fill) LiteLLM.
    Redundant ones are dead weight — delete them. Diverged ones are
    surfaced as warnings so a reviewer re-confirms the pin is still
    intentional (visible in the CI warnings summary).
    """
    findings = detect_override_drift()
    redundant = [f for f in findings if f.kind == "redundant"]
    assert not redundant, "redundant overrides — remove from pricing_overrides.json:\n" + "\n".join(
        f"  {f.key}: {f.detail}" for f in redundant
    )
    for f in findings:
        if f.kind == "diverged":
            warnings.warn(
                f"override pins a price that differs from LiteLLM — re-confirm it's "
                f"still intentional: {f.key} ({f.detail})",
                stacklevel=2,
            )


def test_override_drift_is_frozen_dataclass() -> None:
    """OverrideDrift is a value object — guards accidental mutation."""
    finding = OverrideDrift("k", "redundant", "d")
    try:
        finding.kind = "diverged"  # type: ignore[misc]
    except AttributeError:
        return
    raise AssertionError("OverrideDrift should be frozen")
