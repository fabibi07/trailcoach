"""Consulta al LLM con el Estado del Atleta (nunca FIT ni streams crudos)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from trailcoach.core.config import settings

SYSTEM_PROMPT = (
    "Eres TrailCoach, entrenador experto en running y trail running. Responde siempre en "
    "español, de forma breve y accionable, usando solo el Estado del Atleta entregado "
    "(cargas CTL/ATL/TSB, recuperación, intensidad, umbrales, mejores marcas y carrera "
    "objetivo). No tienes acceso a archivos FIT ni streams. Si faltan datos, dilo y sugiere "
    "qué registrar. No des diagnósticos médicos."
)


# USD por millón de tokens (entrada, salida), según las páginas oficiales de precios.
PRICES_USD_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-sonnet-4-5": (3.0, 15.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4o-mini": (0.15, 0.60),
}
USAGE_LOG = Path("data/coach_usage.jsonl")


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float | None:
    price = PRICES_USD_PER_MTOK.get(model)
    if price is None:
        return None
    return (input_tokens * price[0] + output_tokens * price[1]) / 1_000_000


def _log_usage(usage: dict[str, Any], path: Path = USAGE_LOG) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(usage) + "\n")


def monthly_usage(path: Path = USAGE_LOG, month: str | None = None) -> dict[str, Any]:
    month = month or datetime.now(timezone.utc).strftime("%Y-%m")
    out: dict[str, Any] = {
        "month": month,
        "questions": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cost_usd": 0.0,
    }
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        u = json.loads(line)
        if not str(u.get("ts", "")).startswith(month):
            continue
        out["questions"] += 1
        out["input_tokens"] += u.get("input_tokens", 0)
        out["output_tokens"] += u.get("output_tokens", 0)
        out["cost_usd"] += u.get("cost_usd") or 0.0
    out["cost_usd"] = round(out["cost_usd"], 4)
    return out


def build_prompt(question: str, context: dict[str, Any]) -> str:
    ctx = json.dumps(context, indent=1, default=str, ensure_ascii=False)
    return f"Estado del Atleta ({context.get('as_of')}):\n{ctx}\n\nPregunta: {question}"


def _call(provider: str, model: str, key: str, prompt: str) -> tuple[str, int, int]:
    import httpx

    if provider == "anthropic":
        r = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
            json={
                "model": model,
                "max_tokens": 900,
                "system": SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=90.0,
        )
        r.raise_for_status()
        d = r.json()
        u = d.get("usage", {})
        return str(d["content"][0]["text"]), u.get("input_tokens", 0), u.get("output_tokens", 0)
    r = httpx.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": 900,
        },
        timeout=90.0,
    )
    r.raise_for_status()
    d = r.json()
    u = d.get("usage", {})
    return (
        str(d["choices"][0]["message"]["content"]),
        u.get("prompt_tokens", 0),
        u.get("completion_tokens", 0),
    )


def ask(question: str, context: dict[str, Any]) -> dict[str, Any]:
    import httpx

    provider = settings.ai_provider
    if provider == "anthropic":
        secret = settings.anthropic_api_key
        model = settings.ai_model or "claude-sonnet-4-5"
    else:
        secret = settings.openai_api_key
        model = settings.ai_model or "gpt-4.1-mini"
    prompt = build_prompt(question, context)
    if secret is None:
        return {
            "answer": None,
            "provider": provider,
            "error": "Coach IA sin configurar: falta la API key en .env.",
        }
    try:
        answer, tin, tout = _call(provider, model, secret.get_secret_value(), prompt)
    except httpx.HTTPError as exc:
        return {"answer": None, "provider": provider, "error": f"Error del proveedor: {exc}"}
    usage = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "model": model,
        "input_tokens": tin,
        "output_tokens": tout,
        "cost_usd": cost_usd(model, tin, tout),
    }
    _log_usage(usage)
    return {
        "answer": answer,
        "provider": provider,
        "model": model,
        "usage": usage,
        "month": monthly_usage(),
    }
