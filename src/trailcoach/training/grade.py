"""Grade adjustment for running speed (GAP / NGP)."""

from __future__ import annotations

import polars as pl

# Minetti 2002 energy cost of running polynomial (J kg^-1 m^-1).
# x is grade as a fraction, e.g. 0.1 for +10 %.
_MINETTI_COEFFICIENTS = {
    "x5": 155.4,
    "x4": -30.4,
    "x3": -43.3,
    "x2": 46.3,
    "x1": 19.5,
    "x0": 3.6,
}
MINETTI_FLAT_COST = 3.6
MIN_GRADE_COST = 0.5
GRADE_FRACTION_CLAMP = (-0.45, 0.45)


def minetti_cost(grade_fraction: float) -> float:
    """Energy cost of running for a given grade."""
    x = max(GRADE_FRACTION_CLAMP[0], min(GRADE_FRACTION_CLAMP[1], grade_fraction))
    cost = (
        _MINETTI_COEFFICIENTS["x5"] * x**5
        + _MINETTI_COEFFICIENTS["x4"] * x**4
        + _MINETTI_COEFFICIENTS["x3"] * x**3
        + _MINETTI_COEFFICIENTS["x2"] * x**2
        + _MINETTI_COEFFICIENTS["x1"] * x
        + _MINETTI_COEFFICIENTS["x0"]
    )
    return max(cost, MIN_GRADE_COST)


def grade_adjusted_speed(
    speed_mps: float | None,
    grade_pct: float | None,
) -> float | None:
    """Return equivalent flat speed (m/s) for a speed on a grade."""
    if speed_mps is None or grade_pct is None:
        return speed_mps
    if speed_mps <= 0:
        return 0.0
    factor = minetti_cost(grade_pct / 100.0) / MINETTI_FLAT_COST
    return speed_mps * factor


def add_grade_to_dataframe(df: pl.DataFrame, window_m: float = 100.0) -> pl.DataFrame:
    """Add a robust grade_pct column derived from distance_m and altitude_m."""
    if "grade_pct" in df.columns:
        return df
    if "altitude_m" not in df.columns or "distance_m" not in df.columns:
        return df

    # Smooth altitude in the time domain to remove barometric/GPS noise.
    df = df.with_columns(
        df["altitude_m"].rolling_mean(window_size=30, min_samples=1).alias("_alt_smooth")
    )

    distance = df["distance_m"].to_list()
    altitude = df["_alt_smooth"].to_list()
    n = len(distance)
    grades: list[float | None] = [0.0]
    j = 0
    for i in range(1, n):
        di = float(distance[i] or 0.0)
        ai = float(altitude[i] or 0.0)
        while j < i and float(distance[j] or 0.0) < di - window_m:
            j += 1
        dj = float(distance[j] or 0.0)
        aj = float(altitude[j] or 0.0)
        dx = di - dj
        if dx > 0.1:
            dz = ai - aj
            grade = 100.0 * dz / dx
        else:
            dprev = float(distance[i - 1] or 0.0)
            aprev = float(altitude[i - 1] or 0.0)
            dx2 = di - dprev
            grade = 100.0 * (ai - aprev) / dx2 if dx2 > 0.1 else 0.0
        # Guard against spikes from noisy altitude/distance.
        grades.append(max(-30.0, min(30.0, grade)))

    return df.with_columns(pl.Series("grade_pct", grades, dtype=pl.Float64))
