import asyncio
from decimal import Decimal
from config import binance_client, logger
from strategy import round_price, round_quantity

class BinanceCache:
    def __init__(self):
        self._symbols_info = {}
        self._last_update = 0

    def refresh(self):
        logger.info("Cargando y parseando filtros de Binance Futures en caché...")
        info = binance_client.futures_exchange_info()
        parsed = {}
        for s in info['symbols']:
            sym = s['symbol']
            tick_size = Decimal("0.01")
            step_size = Decimal("0.001")
            min_qty = Decimal("0.001")
            min_notional = Decimal("5.0")

            for f in s.get('filters', []):
                if f['filterType'] == 'PRICE_FILTER':
                    tick_size = Decimal(str(f['tickSize']))
                elif f['filterType'] == 'LOT_SIZE':
                    step_size = Decimal(str(f['stepSize']))
                    min_qty = Decimal(str(f['minQty']))
                elif f['filterType'] == 'MIN_NOTIONAL':
                    min_notional = Decimal(str(f.get('notional', 5.0)))

            parsed[sym] = {
                "tickSize": tick_size,
                "stepSize": step_size,
                "minQty": min_qty,
                "minNotional": min_notional,
            }
        self._symbols_info = parsed
        logger.info(f"Caché de filtros cargada con {len(parsed)} símbolos.")

    def get_filters(self, symbol: str) -> dict | None:
        if not self._symbols_info:
            self.refresh()
        return self._symbols_info.get(symbol)

binance_cache = BinanceCache()

# ─── Operaciones Síncronas Auxiliares ────────────────────────────────────────

def _sync_get_wallet_balance() -> float:
    try:
        acc = binance_client.futures_account()
        for asset in acc.get('assets', []):
            if asset['asset'] == 'USDT':
                return float(asset['walletBalance'])
    except Exception as e:
        logger.error(f"Error consultando balance USDT: {e}")
    return 0.0

def _sync_get_positions(symbol: str):
    pos_long = pos_short = None
    for pos in binance_client.futures_position_information(symbol=symbol):
        amt = float(pos['positionAmt'])
        entry = float(pos['entryPrice'])
        if amt > 0:
            pos_long = {"cantidad": amt, "precio_entrada": entry}
        elif amt < 0:
            pos_short = {"cantidad": abs(amt), "precio_entrada": entry}
    return pos_long, pos_short

def _sync_get_all_active_positions() -> list[dict]:
    """Obtiene todas las posiciones abiertas actualmente en Binance Futures."""
    active = []
    try:
        for pos in binance_client.futures_position_information():
            amt = float(pos['positionAmt'])
            if amt != 0:
                active.append({
                    "symbol": pos['symbol'],
                    "side": "LONG" if amt > 0 else "SHORT",
                    "positionSide": pos.get('positionSide', 'BOTH'),
                    "amount": abs(amt),
                    "entryPrice": float(pos['entryPrice']),
                    "markPrice": float(pos.get('markPrice', 0.0)),
                    "unrealizedProfit": float(pos.get('unRealizedProfit', 0.0)),
                    "leverage": int(pos.get('leverage', 1))
                })
    except Exception as e:
        logger.error(f"Error obteniendo posiciones activas: {e}")
    return active

def _sync_create_limit_order(symbol: str, side: str, qty: float, price: float, pos_side: str):
    filters = binance_cache.get_filters(symbol)
    if not filters:
        raise ValueError(f"Filtros no encontrados para el símbolo {symbol}")
    
    formatted_qty = round_quantity(qty, filters['stepSize'])
    formatted_price = round_price(price, filters['tickSize'])
    
    params = {
        "symbol": symbol,
        "side": side,
        "positionSide": pos_side,
        "type": "LIMIT",
        "quantity": formatted_qty,
        "price": formatted_price,
        "timeInForce": "GTC",
    }
    return binance_client.futures_create_order(**params)

def _sync_create_market_order(symbol: str, side: str, qty: float, pos_side: str):
    filters = binance_cache.get_filters(symbol)
    if not filters:
        raise ValueError(f"Filtros no encontrados para {symbol}")
    
    formatted_qty = round_quantity(qty, filters['stepSize'])
    params = {
        "symbol": symbol,
        "side": side,
        "positionSide": pos_side,
        "type": "MARKET",
        "quantity": formatted_qty,
    }
    return binance_client.futures_create_order(**params)

def _sync_cancel_conflicting_sl_orders(symbol: str, pos_side: str):
    """
    Cancela órdenes abiertas condicionales previas (STOP_MARKET, TAKE_PROFIT_MARKET, STOP)
    o con closePosition=True para el mismo positionSide.
    Esto previene el error Binance -4130:
    'An open stop or take profit order with GTE and closePosition in the direction is existing.'
    """
    try:
        open_orders = binance_client.futures_get_open_orders(symbol=symbol)
        for o in open_orders:
            if o.get("positionSide") == pos_side:
                is_close_pos = o.get("closePosition") is True or str(o.get("closePosition")).lower() == "true"
                is_stop_type = o.get("type") in ["STOP_MARKET", "STOP", "TAKE_PROFIT_MARKET", "TAKE_PROFIT"]
                if is_close_pos or is_stop_type:
                    logger.warning(
                        f"Cancelando orden previa conflictiva {o['orderId']} ({o.get('type')}) en {symbol} [{pos_side}] "
                        f"para liberar slot closePosition y evitar error -4130."
                    )
                    try:
                        binance_client.futures_cancel_order(symbol=symbol, orderId=o["orderId"])
                    except Exception as ce:
                        logger.warning(f"Aviso cancelando orden previa conflictiva {o['orderId']}: {ce}")
    except Exception as e:
        logger.warning(f"Error verificando órdenes abiertas en {symbol}: {e}")

def _sync_create_sl_market_order(symbol: str, side: str, pos_side: str, stop_price: float):
    """
    Coloca una orden nativa STOP_MARKET en Binance con closePosition=True.
    Garantiza ejecución inmediata a nivel del motor de emparejamiento de Binance sin latencia.
    Si Binance arroja APIError(-4130), automáticamente cancela cualquier orden condicional previa
    en esa dirección y reintenta la colocación.
    """
    filters = binance_cache.get_filters(symbol)
    if not filters:
        raise ValueError(f"Filtros no encontrados para {symbol}")
    
    formatted_stop = round_price(stop_price, filters['tickSize'])
    params = {
        "symbol": symbol,
        "side": side,
        "positionSide": pos_side,
        "type": "STOP_MARKET",
        "stopPrice": formatted_stop,
        "closePosition": "true",
    }

    try:
        return binance_client.futures_create_order(**params)
    except Exception as e:
        err_msg = str(e)
        if "-4130" in err_msg or "closePosition in the direction is existing" in err_msg:
            logger.warning(
                f"Error -4130 detectado al armar SL nativo en {symbol} [{pos_side}]. "
                f"Limpiando órdenes stop previas conflictivas y reintentando..."
            )
            _sync_cancel_conflicting_sl_orders(symbol, pos_side)
            # Reintento directo tras liberar el slot en Binance
            return binance_client.futures_create_order(**params)
        raise

def _sync_cancel_order(symbol: str, order_id: int):
    try:
        return binance_client.futures_cancel_order(symbol=symbol, orderId=order_id)
    except Exception as e:
        logger.warning(f"Aviso cancelando orden {order_id} en {symbol}: {e}")
        return None

def _sync_close_all_positions(symbol: str):
    try:
        binance_client.futures_cancel_all_open_orders(symbol=symbol)
    except Exception as e:
        logger.error(f"Error cancelando órdenes previas en {symbol}: {e}")
        
    pos_long, pos_short = _sync_get_positions(symbol)
    if pos_long and pos_long["cantidad"] > 0:
        _sync_create_market_order(symbol, "SELL", pos_long["cantidad"], "LONG")
    if pos_short and pos_short["cantidad"] > 0:
        _sync_create_market_order(symbol, "BUY", pos_short["cantidad"], "SHORT")

# ─── Wrappers Asíncronos Seguros (No Bloquean Asyncio) ───────────────────────

async def async_get_wallet_balance() -> float:
    return await asyncio.to_thread(_sync_get_wallet_balance)

async def async_get_positions(symbol: str):
    return await asyncio.to_thread(_sync_get_positions, symbol)

async def async_get_all_active_positions() -> list[dict]:
    return await asyncio.to_thread(_sync_get_all_active_positions)

async def async_get_ticker_price(symbol: str) -> float:
    def _get():
        return float(binance_client.futures_symbol_ticker(symbol=symbol)["price"])
    return await asyncio.to_thread(_get)

async def async_get_order(symbol: str, order_id: int):
    return await asyncio.to_thread(binance_client.futures_get_order, symbol=symbol, orderId=order_id)

async def async_create_limit_order(symbol: str, side: str, qty: float, price: float, pos_side: str):
    return await asyncio.to_thread(_sync_create_limit_order, symbol, side, qty, price, pos_side)

async def async_create_market_order(symbol: str, side: str, qty: float, pos_side: str):
    return await asyncio.to_thread(_sync_create_market_order, symbol, side, qty, pos_side)

async def async_create_sl_market_order(symbol: str, side: str, pos_side: str, stop_price: float):
    return await asyncio.to_thread(_sync_create_sl_market_order, symbol, side, pos_side, stop_price)

async def async_cancel_conflicting_sl_orders(symbol: str, pos_side: str):
    return await asyncio.to_thread(_sync_cancel_conflicting_sl_orders, symbol, pos_side)

async def async_cancel_order(symbol: str, order_id: int):
    return await asyncio.to_thread(_sync_cancel_order, symbol, order_id)

# Alias para compatibilidad de nomenclatura
async_cancelar_orden = async_cancel_order

async def async_close_all_positions(symbol: str):
    return await asyncio.to_thread(_sync_close_all_positions, symbol)
