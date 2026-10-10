from decimal import Decimal

def round_price(price: float | Decimal, tick_size: Decimal) -> str:
    """Ajusta y redondea un precio al múltiplo exacto de tickSize de Binance."""
    p = Decimal(str(price))
    p_rounded = (p // tick_size) * tick_size
    dec = max(0, -tick_size.as_tuple().exponent)
    return f"{p_rounded:.{dec}f}"

def round_quantity(qty: float | Decimal, step_size: Decimal) -> str:
    """Ajusta y redondea una cantidad al múltiplo exacto de stepSize de Binance."""
    q = Decimal(str(qty))
    q_rounded = (q // step_size) * step_size
    dec = max(0, -step_size.as_tuple().exponent)
    return f"{q_rounded:.{dec}f}"

def validate_notional(qty: float, price: float, min_notional: Decimal) -> bool:
    """Verifica si el valor nocional de la orden supera el mínimo requerido por Binance."""
    val = Decimal(str(qty)) * Decimal(str(price))
    return val >= min_notional

def distancia_pct(p1: float, p2: float) -> float:
    """Calcula la distancia porcentual entre dos precios de entrada."""
    return abs(p1 - p2) / max(p1, p2) * 100

def calcular_ordenes(start_price: float, initial_qty: float, new_dist: float, direction: str):
    """
    Calcula los 4 niveles de la grilla DCA para el desmonte de cobertura.
    direction == 'down': cierre de SHORT (precios decrecientes hacia abajo)
    direction == 'up': cierre de LONG (precios crecientes hacia arriba)
    """
    f = (1 - new_dist / 100) if direction == "down" else (1 + new_dist / 100)
    return [
        {"precio": start_price,        "cantidad": initial_qty},
        {"precio": start_price * f,    "cantidad": initial_qty},
        {"precio": start_price * f**2, "cantidad": initial_qty * 2},
        {"precio": start_price * f**3, "cantidad": initial_qty * 4},
    ]

def calcular_sl(long_qty: float, short_qty: float, total_cerrar: float, ultimo_precio: float, perdida_max_usd: float, direction: str):
    """
    Calcula el precio del Stop Loss para limitar la pérdida máxima en dólares (USD).
    Retorna None si la pérdida máxima no es positiva o el precio resultante no es válido (>0).
    """
    if perdida_max_usd <= 0:
        return None
    if direction == "down":
        net = long_qty - (short_qty - total_cerrar)
        if net <= 0:
            return None
        res = ultimo_precio - (perdida_max_usd / net)
        return res if res > 0 else None
    else:
        net = short_qty - (long_qty - total_cerrar)
        if net <= 0:
            return None
        res = ultimo_precio + (perdida_max_usd / net)
        return res if res > 0 else None
