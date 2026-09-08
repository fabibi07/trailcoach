"""Construcción de prompts para el AI Coach."""

from __future__ import annotations

import json
from typing import Any

SYSTEM_PROMPT = (
    "Eres TrailCoach, un entrenador experto en running y trail running. "
    "Respondes las preguntas del atleta usando solo el Resumen del Estado del "
    "Atleta proporcionado. "
    "NO tienes acceso a archivos FIT crudos, streams por muestra ni trazas GPS. "
    "Mantén las respuestas concisas, accionables y basadas en los datos. "
    "Si faltan datos o la pregunta está fuera de tu alcance, "
    "indícalo y sugiere qué registrar."
)


def build_prompt(question: str, context: dict[str, Any]) -> str:
    """Construye un prompt de un solo turno a partir del contexto del
    Estado del Atleta y la pregunta del atleta."""
    context_json = json.dumps(context, indent=2, default=str, ensure_ascii=False)
    return (
        f"{SYSTEM_PROMPT}\n\n"
        f"Estado del Atleta a fecha de {context.get('as_of')}:\n"
        f"{context_json}\n\n"
        f"Pregunta: {question}\n\n"
        "Respuesta:"
    )
