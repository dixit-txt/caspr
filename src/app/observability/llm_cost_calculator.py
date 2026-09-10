"""llm_cost_calculator.py: Estimate USD cost for an LLM call.

Takes the usage payload shape produced by ``llm_response_logger``
(``metadata.model_name``, ``input_tokens``, ``output_tokens``, ``usage``)
and prices it against ``src/config/llm_model_pricing.json``.

Typical use from the logger::

    cost = estimate_cost_from_payload(payload)
    payload["cost"] = cost
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.core.logging import setup_logging

logger = setup_logging(__name__)

_PRICING_PATH = Path(__file__).resolve().parents[1] / "config" / "llm_model_pricing.json"

# Tokens billed per pricing unit in llm_model_pricing.json ("per_1M_tokens").
_TOKENS_PER_PRICING_UNIT = 1_000_000


@lru_cache(maxsize=1)
def load_pricing() -> dict[str, Any]:
    """Load and cache ``llm_model_pricing.json``."""
    with open(_PRICING_PATH, encoding="utf-8") as f:
        return json.load(f)


def clear_pricing_cache() -> None:
    """Drop the cached pricing file (useful in tests)."""
    load_pricing.cache_clear()


def _as_nonneg_int(value: Any) -> int:
    try:
        n = int(value)
    except TypeError, ValueError:
        return 0
    return max(n, 0)


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except TypeError, ValueError:
        return default


def _tokens_cost(tokens: int, price_per_1m: float | None) -> float:
    if not tokens or price_per_1m is None:
        return 0.0
    return (tokens / _TOKENS_PER_PRICING_UNIT) * float(price_per_1m)


def _resolve_model_pricing(model_name: str) -> dict[str, Any] | None:
    """Look up a model entry, following ``same_as`` aliases when present."""
    models = load_pricing().get("models") or {}
    key = str(model_name or "").strip()
    if not key:
        return None

    entry = models.get(key)
    if entry is None:
        # Case-insensitive / alias fallback
        lower = key.lower()
        for name, candidate in models.items():
            if name.lower() == lower:
                entry = candidate
                break
            aliases = candidate.get("aliases") or []
            if any(str(a).lower() == lower for a in aliases):
                entry = candidate
                break

    if not isinstance(entry, dict):
        return None

    same_as = entry.get("same_as")
    if same_as and same_as in models and isinstance(models[same_as], dict):
        # Prefer the canonical row but keep any overrides from the alias row.
        merged = dict(models[same_as])
        merged.update({k: v for k, v in entry.items() if k != "same_as"})
        return merged
    return entry


def _extract_cached_input_tokens(usage: Any) -> int:
    """Best-effort cached/prompt-cache token count from provider usage blobs."""
    if not isinstance(usage, dict):
        return 0

    # Flatten one level of common wrappers (usage / usage_metadata).
    candidates = [usage]
    for key in ("usage", "usage_metadata", "usagemetadata"):
        block = usage.get(key)
        if isinstance(block, dict):
            candidates.append(block)

    for block in candidates:
        for key in (
            "cache_read_input_tokens",
            "cached_tokens",
            "cache_read_tokens",
        ):
            if key in block and block[key] is not None:
                return _as_nonneg_int(block[key])

        for details_key in (
            "input_tokens_details",
            "prompt_tokens_details",
            "input_token_details",
        ):
            details = block.get(details_key)
            if isinstance(details, dict) and details.get("cached_tokens") is not None:
                return _as_nonneg_int(details.get("cached_tokens"))

    return 0


def _extract_num_images(usage: Any) -> int:
    if not isinstance(usage, dict):
        return 0
    if usage.get("num_images") is not None:
        return _as_nonneg_int(usage.get("num_images"))
    for field in ("generated_images", "data"):
        value = usage.get(field)
        if isinstance(value, list):
            return len(value)
    return 0


def _extract_tool_usage(usage: Any) -> dict[str, Any]:
    """Return the OpenAI-style ``tool_usage`` dict from a logger usage blob."""
    if not isinstance(usage, dict):
        return {}
    tu = usage.get("tool_usage")
    if isinstance(tu, dict):
        return tu
    nested = usage.get("usage")
    if isinstance(nested, dict) and isinstance(nested.get("tool_usage"), dict):
        return nested["tool_usage"]
    return {}


def _extract_sonar_context_tier(usage: Any) -> str | None:
    """Map Perplexity ``search_context_size`` to pricing keys like medium_context."""
    if not isinstance(usage, dict):
        return None
    candidates = [usage]
    nested = usage.get("usage")
    if isinstance(nested, dict):
        candidates.append(nested)
    for block in candidates:
        size = block.get("search_context_size")
        if isinstance(size, str) and size.strip():
            key = size.strip().lower()
            if key.endswith("_context"):
                return key
            return f"{key}_context"
    return None


def _estimate_openai_tool_fees(
    tool_usage: dict[str, Any],
    *,
    model_name: str,
) -> dict[str, Any]:
    """Price OpenAI built-in tools from ``usage.tool_usage``."""
    tools_cfg = (load_pricing().get("tools") or {}).get("openai") or {}
    breakdown: dict[str, Any] = {}
    total = 0.0

    # OpenAI still reports this as usage.tool_usage.web_search — keep reading
    # that key — but store/display it as Learning Brain (product name).
    web = tool_usage.get("web_search") or tool_usage.get("learning_brain")
    if isinstance(web, dict):
        n = _as_nonneg_int(web.get("num_requests"))
        if n:
            rate = _as_float(
                (tools_cfg.get("web_search") or tools_cfg.get("learning_brain") or {}).get(
                    "cost_per_call"
                ),
                0.01,
            )
            fee = n * rate
            total += fee
            breakdown["learning_brain"] = {
                "num_requests": n,
                "cost_per_call": rate,
                "cost_usd": round(fee, 6),
            }

    file_search = tool_usage.get("file_search")
    if isinstance(file_search, dict):
        n = _as_nonneg_int(
            file_search.get("num_requests")
            or file_search.get("num_calls")
            or file_search.get("calls")
        )
        if n:
            rate = _as_float((tools_cfg.get("file_search") or {}).get("cost_per_call"), 0.0025)
            fee = n * rate
            total += fee
            breakdown["file_search"] = {
                "num_requests": n,
                "cost_per_call": rate,
                "cost_usd": round(fee, 6),
            }

    image_gen = tool_usage.get("image_gen")
    if isinstance(image_gen, dict):
        in_tok = _as_nonneg_int(image_gen.get("input_tokens"))
        out_tok = _as_nonneg_int(image_gen.get("output_tokens"))
        if in_tok or out_tok:
            img_model = (tools_cfg.get("image_gen") or {}).get(
                "same_rates_as_model"
            ) or "gpt-image-2"
            img_cost = estimate_llm_cost(
                model_name=str(img_model),
                input_tokens=in_tok,
                output_tokens=out_tok,
                usage=image_gen,
            )
            fee = _as_float(img_cost.get("estimated_cost_usd"), 0.0)
            total += fee
            breakdown["image_gen"] = {
                "input_tokens": in_tok,
                "output_tokens": out_tok,
                "priced_as": img_model,
                "cost_usd": round(fee, 6),
            }

    return {"tool_cost_usd": round(total, 6), "tool_breakdown": breakdown, "model_name": model_name}


def _pick_per_image_rate(
    cost_per_image: Any,
    *,
    size: str | None = None,
    quality: str | None = None,
) -> float | None:
    """Resolve a scalar per-image USD rate from float or size/quality maps."""
    if isinstance(cost_per_image, (int, float)):
        return float(cost_per_image)
    if not isinstance(cost_per_image, dict) or not cost_per_image:
        return None

    # Prefer an exact size / quality_size key when provided.
    if size:
        if size in cost_per_image:
            return _as_float(cost_per_image[size])
        # dall-e-3 style keys: standard_1024x1024, hd_1024x1024, …
        q = (quality or "standard").lower()
        for key, val in cost_per_image.items():
            key_l = str(key).lower()
            if size.lower() in key_l and q in key_l:
                return _as_float(val)

    # Fallback: cheapest listed rate (conservative default for unknowns).
    try:
        return min(float(v) for v in cost_per_image.values() if isinstance(v, (int, float)))
    except ValueError:
        return None


def _pick_tiered_rate(
    pricing: dict[str, Any],
    field: str,
    long_ctx: bool,
) -> float | None:
    """Resolve a rate that may use leq/gt threshold suffixes."""
    if long_ctx:
        return pricing.get(f"{field}_gt_threshold") or pricing.get(f"{field}_gt_200k")
    return (
        pricing.get(f"{field}_leq_threshold")
        or pricing.get(f"{field}_leq_200k")
        or pricing.get(field)
    )


def _has_tiered_token_pricing(pricing: dict[str, Any]) -> bool:
    tier_keys = (
        "input_per_1m_leq_200k",
        "input_per_1m_gt_200k",
        "input_per_1m_leq_threshold",
        "input_per_1m_gt_threshold",
    )
    return any(pricing.get(k) is not None for k in tier_keys)


def _token_rates_for_model(
    pricing: dict[str, Any],
    input_tokens: int,
) -> dict[str, float | None]:
    """Pick input/output/cached rates, including long-context tiered models."""
    billing = pricing.get("billing")

    if _has_tiered_token_pricing(pricing):
        threshold = pricing.get("context_tier_threshold_tokens", 200_000)
        long_ctx = input_tokens > threshold
        return {
            "input_per_1m": _pick_tiered_rate(pricing, "input_per_1m", long_ctx),
            "output_per_1m": _pick_tiered_rate(pricing, "output_per_1m", long_ctx),
            "cached_input_per_1m": _pick_tiered_rate(pricing, "cached_input_per_1m", long_ctx),
        }

    # Nano Banana / Gemini image models: text+image input, text/thinking output.
    if billing == "tokens_and_image_output":
        return {
            "input_per_1m": pricing.get("input_text_image_per_1m") or pricing.get("input_per_1m"),
            "output_per_1m": pricing.get("output_text_thinking_per_1m")
            or pricing.get("output_per_1m"),
            "cached_input_per_1m": pricing.get("cached_input_per_1m"),
        }

    # gpt-image-2 style: prefer text input rate when only aggregate tokens known.
    if pricing.get("text_input_per_1m") is not None and pricing.get("input_per_1m") is None:
        return {
            "input_per_1m": pricing.get("text_input_per_1m"),
            "output_per_1m": pricing.get("image_output_per_1m") or pricing.get("output_per_1m"),
            "cached_input_per_1m": pricing.get("cached_text_input_per_1m")
            or pricing.get("cached_input_per_1m"),
        }

    return {
        "input_per_1m": pricing.get("input_per_1m"),
        "output_per_1m": pricing.get("output_per_1m"),
        "cached_input_per_1m": pricing.get("cached_input_per_1m")
        or pricing.get("cache_hit_per_1m"),
    }


def estimate_llm_cost(
    *,
    model_name: str,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    usage: dict[str, Any] | None = None,
    num_images: int | None = None,
    image_size: str | None = None,
    image_quality: str | None = None,
    sonar_context_tier: str = "medium_context",
    sonar_pro_search: bool = False,
) -> dict[str, Any]:
    """Estimate USD cost for one LLM call.

    Returns a dict always containing ``estimated_cost_usd`` (float|None) plus
    pricing metadata. When the model is unknown or required inputs are missing,
    ``estimated_cost_usd`` is ``None`` and ``error`` explains why.
    """
    currency = (load_pricing().get("meta") or {}).get("currency", "USD")
    in_tok = _as_nonneg_int(input_tokens)
    out_tok = _as_nonneg_int(output_tokens)
    usage = usage if isinstance(usage, dict) else {}
    cached_tok = _extract_cached_input_tokens(usage)
    # Prefer Perplexity search_context_size from the usage blob when present.
    detected_sonar_tier = _extract_sonar_context_tier(usage)
    if detected_sonar_tier:
        sonar_context_tier = detected_sonar_tier
    tool_usage = _extract_tool_usage(usage)
    tool_fees = (
        _estimate_openai_tool_fees(tool_usage, model_name=model_name)
        if tool_usage
        else {
            "tool_cost_usd": 0.0,
            "tool_breakdown": {},
        }
    )

    # Prefer an already-computed image cost from the logger.
    if usage.get("estimated_cost_usd") is not None and usage.get("num_images") is not None:
        return {
            "estimated_cost_usd": round(_as_float(usage["estimated_cost_usd"]), 6),
            "currency": currency,
            "model_name": model_name,
            "billing": "per_image",
            "source": "usage.estimated_cost_usd",
            "breakdown": {
                "num_images": _as_nonneg_int(usage.get("num_images")),
                "cost_per_image_usd": usage.get("cost_per_image_usd"),
            },
        }

    pricing = _resolve_model_pricing(model_name)
    if pricing is None:
        return {
            "estimated_cost_usd": None,
            "currency": currency,
            "model_name": model_name,
            "billing": None,
            "error": f"No pricing entry for model_name={model_name!r}",
        }

    billing = pricing.get("billing") or "tokens"
    breakdown: dict[str, Any] = {
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "cached_input_tokens": cached_tok,
    }

    # --- per-image billing (Imagen / DALL·E) ---
    if billing == "per_image":
        images = (
            _as_nonneg_int(num_images) if num_images is not None else _extract_num_images(usage)
        )
        rate = _pick_per_image_rate(
            pricing.get("cost_per_image_usd"),
            size=image_size,
            quality=image_quality,
        )
        if rate is None:
            return {
                "estimated_cost_usd": None,
                "currency": currency,
                "model_name": model_name,
                "billing": billing,
                "error": "Could not resolve cost_per_image_usd",
                "breakdown": breakdown,
            }
        cost = images * rate
        breakdown.update({"num_images": images, "cost_per_image_usd": rate})
        return {
            "estimated_cost_usd": round(cost, 6),
            "currency": currency,
            "billing": billing,
            "breakdown": breakdown,
        }

    rates = _token_rates_for_model(pricing, in_tok)
    billable_input = max(in_tok - cached_tok, 0)
    input_cost = _tokens_cost(billable_input, rates.get("input_per_1m"))
    cached_cost = _tokens_cost(cached_tok, rates.get("cached_input_per_1m"))
    # If cache rate is unknown, bill cached tokens at full input rate.
    if cached_tok and rates.get("cached_input_per_1m") is None:
        cached_cost = _tokens_cost(cached_tok, rates.get("input_per_1m"))
    output_cost = _tokens_cost(out_tok, rates.get("output_per_1m"))

    breakdown.update(
        {
            "billable_input_tokens": billable_input,
            "input_per_1m": rates.get("input_per_1m"),
            "cached_input_per_1m": rates.get("cached_input_per_1m"),
            "output_per_1m": rates.get("output_per_1m"),
            "input_cost_usd": round(input_cost, 6),
            "cached_input_cost_usd": round(cached_cost, 6),
            "output_cost_usd": round(output_cost, 6),
        }
    )

    request_fee = 0.0
    if billing == "tokens_plus_request_fee":
        fee_table_key = (
            "pro_search_request_fee_per_1k" if sonar_pro_search else "request_fee_per_1k"
        )
        fee_table = pricing.get(fee_table_key) or {}
        # request_fee_per_1k is USD per 1k requests → per-request = rate / 1000
        per_1k = fee_table.get(sonar_context_tier)
        if per_1k is None and isinstance(fee_table, dict) and fee_table:
            per_1k = fee_table.get("medium_context") or next(iter(fee_table.values()))
        if per_1k is not None:
            request_fee = float(per_1k) / 1000.0
            breakdown["request_fee_usd"] = round(request_fee, 6)
            breakdown["sonar_context_tier"] = sonar_context_tier
            breakdown["sonar_pro_search"] = sonar_pro_search

    # Optional approximate image add-on when only approx rates exist and no tokens.
    image_addon = 0.0
    if billing == "tokens_and_image_output" and in_tok == 0 and out_tok == 0:
        approx = pricing.get("approx_cost_per_1k_2k_image_usd")
        images = (
            _as_nonneg_int(num_images) if num_images is not None else _extract_num_images(usage)
        ) or 1
        if approx is not None:
            image_addon = images * float(approx)
            breakdown["approx_image_cost_usd"] = round(image_addon, 6)
            breakdown["num_images"] = images

    tool_cost = _as_float(tool_fees.get("tool_cost_usd"), 0.0)
    if tool_fees.get("tool_breakdown"):
        breakdown["tools"] = tool_fees["tool_breakdown"]
        breakdown["tool_cost_usd"] = round(tool_cost, 6)

    total = input_cost + cached_cost + output_cost + request_fee + image_addon + tool_cost

    if (
        rates.get("input_per_1m") is None
        and rates.get("output_per_1m") is None
        and request_fee == 0.0
        and image_addon == 0.0
        and tool_cost == 0.0
    ):
        return {
            "estimated_cost_usd": None,
            "currency": currency,
            "model_name": model_name,
            "billing": billing,
            "error": "Pricing entry has no usable token/image rates",
            "breakdown": breakdown,
        }

    return {
        "estimated_cost_usd": round(total, 6),
        "currency": currency,
        "model_name": model_name,
        "billing": billing,
        "breakdown": breakdown,
    }


def estimate_cost_from_payload(payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    """Estimate cost from a ``save_raw_llm_response`` / logger payload dict.

    Expected shape::

        {
          "metadata": {"model_name": "...", ...},
          "usage": {...},
          "input_tokens": int | None,
          "output_tokens": int | None,
        }
    """
    if not isinstance(payload, dict):
        return {
            "estimated_cost_usd": None,
            "currency": "USD",
            "error": "payload must be a dict",
        }

    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    model_name = metadata.get("model_name") or payload.get("model_name") or ""
    return estimate_llm_cost(
        model_name=str(model_name),
        input_tokens=payload.get("input_tokens"),
        output_tokens=payload.get("output_tokens"),
        usage=payload.get("usage") if isinstance(payload.get("usage"), dict) else {},
        **kwargs,
    )


def attach_cost_to_payload(payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    """Mutate ``payload`` in place: set ``payload["cost"]`` and mirror USD on usage.

    Also writes ``usage["estimated_cost_usd"]`` when a numeric estimate is available
    so downstream persistence of ``usage_metadata`` carries the cost.
    """
    cost = estimate_cost_from_payload(payload, **kwargs)
    payload["cost"] = cost

    usage = payload.get("usage")
    if isinstance(usage, dict) and cost.get("estimated_cost_usd") is not None:
        usage["estimated_cost_usd"] = cost["estimated_cost_usd"]
        usage["cost_currency"] = cost.get("currency", "USD")

    return payload
