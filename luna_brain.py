"""
Luna Brain: digital neuron layer used as Luna's main decision/routing "chip".
Spiking-style neurons (integrate-and-fire) in code — no living cells, same idea:
inputs integrate, fire when threshold is reached, then reset. Used to gate
memory (should we remember this?) and optionally route or weight responses.

Layers:
  - Input encoding: message → simple features (length, keywords, "remember" intent).
  - Neuron layer: a few units that integrate these features and "spike" when important.
  - Output: decisions e.g. should_remember, should_add_core (high importance).
"""

import re
from typing import Any

# --- Simple integrate-and-fire neuron ---

_DECAY = 0.92   # leak per step (simplified: we run one "step" per call)
_THRESHOLD = 0.7
_RESET_AFTER_SPIKE = 0.0


def _sigmoid(x: float) -> float:
    """Squash to (0, 1)."""
    try:
        import math
        return 1.0 / (1.0 + math.exp(-max(-20, min(20, x))))
    except Exception:
        return 0.5


def _neuron_step(voltage: float, input_current: float, decay: float = _DECAY,
                 threshold: float = _THRESHOLD) -> tuple[float, bool]:
    """
    Single integrate-and-fire step.
    Returns (new_voltage, did_spike).
    """
    v = voltage * decay + input_current
    if v >= threshold:
        return _RESET_AFTER_SPIKE, True
    return max(0.0, v), False


# --- Feature extraction (no LLM) ---

def _features(msg: str, context: dict[str, Any] | None = None) -> dict[str, float]:
    """Turn user message (and optional context) into a small feature vector for the brain."""
    msg = (msg or "").strip()
    low = msg.lower()
    ctx = context or {}

    f = {
        "length_norm": min(1.0, len(msg) / 500.0),
        "has_remember": 1.0 if re.search(r"\b(?:remember|don't forget|keep in mind|memorize|save this|note that|my name is|i'm called|call me)\b", low) else 0.0,
        "has_fact": 1.0 if re.search(r"\b(?:i (?:like|love|hate|prefer|work|live)|my (?:name|birthday|job|dog|cat|wife|husband|kid)|i'm from|i live in)\b", low) else 0.0,
        "has_question": 0.7 if "?" in msg else 0.0,
        "explicit_core": 1.0 if re.search(r"\b(?:always remember|essential|permanent|important to me)\b", low) else 0.0,
    }
    # Optional: conversation length / recency from context
    if isinstance(ctx.get("recent_count"), int):
        f["recent_activity"] = min(1.0, ctx["recent_count"] / 10.0) * 0.2
    else:
        f["recent_activity"] = 0.0
    return f


# --- Brain state (voltage per neuron); keyed by scope so we don't mix users ---

_brain_voltages: dict[str, list[float]] = {}
_NUM_NEURONS = 4  # e.g. 0: general importance, 1: remember intent, 2: core, 3: routing


def _get_voltages(scope: str) -> list[float]:
    if scope not in _brain_voltages:
        _brain_voltages[scope] = [0.0] * _NUM_NEURONS
    return _brain_voltages[scope]


def _weights_for_remember() -> list[dict[str, float]]:
    """Weight matrix: each neuron i gets input from features. W[i][feat] = weight."""
    return [
        {"length_norm": 0.2, "has_remember": 0.9, "has_fact": 0.8, "has_question": -0.1, "explicit_core": 0.6, "recent_activity": 0.1},
        {"length_norm": 0.1, "has_remember": 0.95, "has_fact": 0.3, "has_question": 0.0, "explicit_core": 0.2, "recent_activity": 0.0},
        {"length_norm": 0.0, "has_remember": 0.2, "has_fact": 0.1, "has_question": 0.0, "explicit_core": 0.98, "recent_activity": 0.0},
        {"length_norm": 0.15, "has_remember": 0.1, "has_fact": 0.2, "has_question": 0.3, "explicit_core": 0.1, "recent_activity": 0.2},
    ]


def brain_step(scope: str, user_message: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
    """
    Run one brain step: encode message → feed into neurons → return decisions.
    Returns e.g. {"should_remember": bool, "should_add_core": bool, "spikes": [bool,...]}.
    """
    feat = _features(user_message, context)
    weights = _weights_for_remember()
    volts = _get_voltages(scope)
    spikes = [False] * _NUM_NEURONS

    for i in range(_NUM_NEURONS):
        w = weights[i]
        current = sum(feat.get(k, 0.0) * w.get(k, 0.0) for k in feat)
        current = _sigmoid(current) * 0.5  # scale into reasonable input
        volts[i], spikes[i] = _neuron_step(volts[i], current)

    # Decisions from spikes
    # Neuron 0 & 1: general "should we store this in long-term?"
    should_remember = spikes[0] or spikes[1] or feat.get("has_remember", 0) > 0.5 or feat.get("has_fact", 0) > 0.5
    # Neuron 2: core (always-remember) tier
    should_add_core = spikes[2] or feat.get("explicit_core", 0) > 0.5

    return {
        "should_remember": should_remember,
        "should_add_core": should_add_core,
        "spikes": spikes,
        "features": feat,
    }


def brain_should_remember(scope: str, user_message: str, context: dict[str, Any] | None = None) -> bool:
    """Convenience: True if the brain says we should store this in memory (long-term or core)."""
    out = brain_step(scope, user_message, context)
    return out["should_remember"] or out["should_add_core"]
