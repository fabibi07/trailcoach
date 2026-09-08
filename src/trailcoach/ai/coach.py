"""Capa de ejecución del AI Coach."""

from __future__ import annotations

from typing import Any

import httpx
from sqlalchemy.orm import Session

from trailcoach.ai.context import build_athlete_state
from trailcoach.ai.prompts import SYSTEM_PROMPT, build_prompt
from trailcoach.core.config import settings
from trailcoach.db.models import Athlete


def _call_openai(prompt: str, api_key: str, model: str) -> dict[str, Any]:
    try:
        resp = httpx.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.2,
                "max_tokens": 800,
            },
            timeout=60.0,
        )
        resp.raise_for_status()
        data = resp.json()
        answer = data["choices"][0]["message"]["content"]
        return {
            "answer": answer,
            "model": model,
            "provider": "openai",
            "usage": data.get("usage"),
        }
    except httpx.HTTPError as exc:
        return {
            "answer": None,
            "error": f"Falló la petición a OpenAI: {exc}",
            "provider": "openai",
        }


def _call_anthropic(prompt: str, api_key: str, model: str) -> dict[str, Any]:
    try:
        resp = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": 800,
                "system": SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=60.0,
        )
        resp.raise_for_status()
        data = resp.json()
        answer = data["content"][0]["text"]
        return {
            "answer": answer,
            "model": model,
            "provider": "anthropic",
            "usage": data.get("usage"),
        }
    except httpx.HTTPError as exc:
        return {
            "answer": None,
            "error": f"Falló la petición a Anthropic: {exc}",
            "provider": "anthropic",
        }


def ask(
    db: Session,
    athlete: Athlete,
    question: str,
    provider: str | None = None,
) -> dict[str, Any]:
    """Construye el contexto y consulta al entrenador LLM configurado.

    Si no hay una API key configurada, devuelve el prompt/contexto en modo
    dry-run para que quien llama pueda inspeccionar lo que se enviaría al LLM.
    """
    context = build_athlete_state(db, athlete)
    prompt = build_prompt(question, context)

    chosen = provider or settings.ai_provider or "openai"
    model = settings.ai_model or "gpt-4o-mini"

    if chosen == "openai":
        api_key = settings.openai_api_key
        caller = _call_openai
    elif chosen == "anthropic":
        api_key = settings.anthropic_api_key
        model = model if "claude" in model else "claude-3-5-sonnet-20241022"
        caller = _call_anthropic
    else:
        return {
            "question": question,
            "context": context,
            "prompt": prompt,
            "provider": chosen,
            "model": model,
            "dry_run": True,
            "answer": None,
            "error": f"Proveedor desconocido: {chosen}",
        }

    if not api_key:
        return {
            "question": question,
            "context": context,
            "prompt": prompt,
            "provider": chosen,
            "model": model,
            "dry_run": True,
            "answer": None,
            "error": f"No hay una API key configurada para el proveedor: {chosen}",
        }

    result = caller(prompt, api_key, model)
    return {
        "question": question,
        "context": context,
        "prompt": prompt,
        **result,
        "dry_run": False,
    }
