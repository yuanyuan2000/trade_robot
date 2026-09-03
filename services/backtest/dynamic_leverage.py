from __future__ import annotations

import math


def calculate_dynamic_symbol_leverage(
    volatility_percent: float,
    *,
    stress_days: int,
    max_loss_percent: float,
    max_leverage: float,
) -> dict:
    """Calculate the configured symbol leverage from annualized volatility.

    ``volatility_percent`` and ``max_loss_percent`` deliberately use the same
    percentage units.  Flooring, rather than rounding, ensures the configured
    stress budget is never exceeded merely because of decimal presentation.
    The 1x floor is a product rule; when it binds the stress budget can be
    exceeded and ``risk_constraint_satisfied`` reports that fact.
    """
    volatility = float(volatility_percent)
    days = int(stress_days)
    loss = float(max_loss_percent)
    maximum = float(max_leverage)
    if not math.isfinite(volatility) or volatility < 0:
        raise ValueError("VOLAT must be a finite non-negative percentage.")
    if days < 1:
        raise ValueError("stress_days must be positive.")
    if not math.isfinite(loss) or loss <= 0:
        raise ValueError("max_loss_percent must be positive.")
    if not math.isfinite(maximum) or maximum < 1:
        raise ValueError("max_leverage must be at least 1.")

    stress_factor = 3 * math.sqrt(days / 252)
    stress_loss = volatility * stress_factor
    raw_leverage = math.inf if stress_loss == 0 else loss / stress_loss
    bounded = min(maximum, max(1.0, raw_leverage))
    leverage = max(1.0, math.floor((bounded + 1e-12) * 10) / 10)
    return {
        "volatility": volatility,
        "stress_factor": stress_factor,
        "stress_loss_percent": stress_loss,
        "raw_leverage": raw_leverage,
        "leverage": leverage,
        "risk_constraint_satisfied": leverage * stress_loss <= loss + 1e-12,
    }
