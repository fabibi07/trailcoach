"""Interpretación del estado del atleta en recomendaciones legibles.

Funciones puras: reciben métricas ya calculadas (PMC, carga, umbrales) y
devuelven mensajes cortos para el dashboard y el AI Coach.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date

ACWR_HIGH = 1.5
ACWR_CAUTION = 1.3
ACWR_LOW = 0.8
RAMP_HIGH = 8.0
RAMP_FAST = 5.0
RAMP_DETRAINING = -5.0
PACE_DROP_PCT = 5.0
DAYS_WITHOUT_TRAINING = 5


@dataclass
class Insight:
    kind: str
    level: str  # ok | info | warn | alert
    title: str
    message: str


def form_insight(tsb: float | None) -> Insight | None:
    if tsb is None:
        return None
    if tsb < -30:
        return Insight(
            "form",
            "alert",
            "Sobrecarga",
            f"Forma {tsb:.0f}: acumulas mucha más fatiga que tu fitness. "
            "Baja la intensidad 2-3 días y prioriza el sueño.",
        )
    if tsb < -10:
        return Insight(
            "form",
            "ok",
            "Bloque de carga",
            f"Forma {tsb:.0f}: estás cansado, pero es la fatiga normal de un bloque "
            "que construye fitness. Mantén, sin sumar más intensidad.",
        )
    if tsb <= 10:
        return Insight(
            "form",
            "ok",
            "Equilibrio",
            f"Forma {tsb:.0f}: fatiga y fitness están equilibrados. "
            "Puedes entrenar con normalidad, incluida una sesión de calidad.",
        )
    if tsb <= 25:
        return Insight(
            "form",
            "info",
            "Fresco",
            f"Forma {tsb:.0f}: estás descansado. Buen momento para competir o hacer un test.",
        )
    return Insight(
        "form",
        "warn",
        "Muy descansado",
        f"Forma {tsb:.0f}: llevas varios días con poca carga y empiezas a perder fitness. "
        "Si no estás en taper, retoma el volumen.",
    )


def acwr_insight(load_7d: float | None, load_28d: float | None) -> Insight | None:
    if not load_7d or not load_28d:
        return None
    acwr = load_7d / (load_28d / 4)
    pct = (acwr - 1) * 100
    base = (
        f"Carga de 7 días: {load_7d:.0f} (tu promedio semanal del último mes: {load_28d / 4:.0f})."
    )
    if acwr > ACWR_HIGH:
        return Insight(
            "acwr",
            "alert",
            "Subida brusca de carga",
            f"{base} Es un {pct:.0f} % más: mayor riesgo de lesión. Aliviana la próxima semana.",
        )
    if acwr > ACWR_CAUTION:
        return Insight(
            "acwr",
            "warn",
            "Carga en aumento",
            f"{base} Es un {pct:.0f} % más. No subas más esta semana.",
        )
    if acwr < ACWR_LOW:
        return Insight(
            "acwr",
            "info",
            "Semana liviana",
            f"{base} Es un {-pct:.0f} % menos: sirve como descarga, pero no la alargues.",
        )
    return Insight(
        "acwr",
        "ok",
        "Carga estable",
        f"{base} Dentro del rango seguro (±30 %).",
    )


def ramp_insight(ramp_rate_7d: float | None) -> Insight | None:
    if ramp_rate_7d is None:
        return None
    r = ramp_rate_7d
    if r > RAMP_HIGH:
        return Insight(
            "ramp",
            "alert",
            "Fitness subiendo muy rápido",
            f"Tu fitness subió {r:.1f} puntos en 7 días. Más de 8 por semana suele terminar "
            "en lesión o sobreentrenamiento.",
        )
    if r > RAMP_FAST:
        return Insight(
            "ramp",
            "warn",
            "Progresión rápida",
            f"Tu fitness subió {r:.1f} puntos en 7 días. Sostenible solo por 2-3 semanas.",
        )
    if r >= 0:
        return Insight(
            "ramp",
            "ok",
            "Progresión sana",
            f"Tu fitness subió {r:.1f} puntos en 7 días (lo sano es entre 0 y 5).",
        )
    if r < RAMP_DETRAINING:
        return Insight(
            "ramp",
            "warn",
            "Perdiendo fitness",
            f"Tu fitness bajó {-r:.1f} puntos en 7 días.",
        )
    return Insight(
        "ramp",
        "info",
        "Fitness bajando levemente",
        f"Tu fitness bajó {-r:.1f} puntos en 7 días. Normal en una semana de descarga.",
    )


def threshold_pace_insight(
    current_mps: float | None, best_365d_mps: float | None, source: str | None
) -> Insight | None:
    if not current_mps or not best_365d_mps or source == "manual":
        return None
    drop = (1 - current_mps / best_365d_mps) * 100
    if drop < PACE_DROP_PCT:
        return None
    return Insight(
        "threshold_pace",
        "warn",
        "Ritmo umbral posiblemente desactualizado",
        f"Tu ritmo umbral actual ({_pace(current_mps)}/km) es {drop:.0f} % más lento que tu mejor "
        f"valor del último año ({_pace(best_365d_mps)}/km). Si solo hiciste intervalos, la carga "
        "de pista aparece inflada. Haz un test de 30 min para corregirlo.",
    )


def inactivity_insight(last_activity: date | None, today: date) -> Insight | None:
    if last_activity is None:
        return None
    days = (today - last_activity).days
    if days < DAYS_WITHOUT_TRAINING:
        return None
    return Insight(
        "inactivity",
        "info",
        "Sin actividades recientes",
        f"Tu última actividad fue hace {days} días. Si entrenaste, revisa que el reloj "
        "haya sincronizado con Garmin Connect.",
    )


def build_insights(
    *,
    today: date,
    tsb: float | None,
    ramp_rate_7d: float | None,
    load_7d: float | None,
    load_28d: float | None,
    ftp_pace_mps: float | None = None,
    ftp_pace_best_365d_mps: float | None = None,
    ftp_pace_source: str | None = None,
    last_activity: date | None = None,
) -> list[dict[str, str]]:
    """Recomendaciones del día, de la más a la menos urgente."""
    order = {"alert": 0, "warn": 1, "info": 2, "ok": 3}
    items = [
        form_insight(tsb),
        acwr_insight(load_7d, load_28d),
        ramp_insight(ramp_rate_7d),
        threshold_pace_insight(ftp_pace_mps, ftp_pace_best_365d_mps, ftp_pace_source),
        inactivity_insight(last_activity, today),
    ]
    found = [i for i in items if i is not None]
    found.sort(key=lambda i: order[i.level])
    return [asdict(i) for i in found]


def _pace(mps: float) -> str:
    sec = round(1000 / mps)
    return f"{sec // 60}:{sec % 60:02d}"
