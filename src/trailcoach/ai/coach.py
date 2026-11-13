"""Consulta al LLM con el Estado del Atleta (nunca FIT ni streams crudos)."""

from __future__ import annotations

import json
from typing import Any

import httpx

from trailcoach.core.config import settings

SYSTEM_PROMPT = (
    "Eres TrailCoach, entrenador experto en running y trail running. Responde siempre en "
    "español, de forma breve y accionable, usando solo el Estado del Atleta entregado "
    "(cargas CTL/ATL/TSB, recuperación, intensidad, umbrales, mejores marcas y carrera "
    "objetivo). No tienes acceso a archivos FIT ni streams. Si faltan datos, dilo y sugiere "
    "qué registrar. No des diagnósticos médicos."
)


def build_prompt(question: str, context: dict[str, Any]) -> str:
    ctx = json.dumps(context, indent=1, default=str, ensure_ascii=False)
    return f"Estado del Atleta ({context.get('as_of')}):\n{ctx}\n\nPregunta: {question}"


def _call(provider: str, model: str, key: str, prompt: str) -> str:
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
        return str(r.json()["content"][0]["text"])
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
    return str(r.json()["choices"][0]["message"]["content"])


def ask(question: str, context: dict[str, Any]) -> dict[str, Any]:
    provider = settings.ai_provider
    if provider == "anthropic":
        secret = settings.anthropic_api_key
        model = settings.ai_model or "claude-sonnet-4-5"
    else:
        secret = settings.openai_api_key
        model = settings.ai_model or "gpt-4o-mini"
    prompt = build_prompt(question, context)
    if secret is None:
        return {
            "answer": None,
            "provider": provider,
            "error": "Coach IA sin configurar: falta la API key en .env.",
        }
    try:
        answer = _call(provider, model, secret.get_secret_value(), prompt)
    except httpx.HTTPError as exc:
        return {"answer": None, "provider": provider, "error": f"Error del proveedor: {exc}"}
    return {"answer": answer, "provider": provider, "model": model}
