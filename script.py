import os
import asyncio
from decimal import Decimal
from dotenv import load_dotenv
from binance.client import Client
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

load_dotenv()

# Variables de entorno
API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_SECRET_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ALLOWED_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID") # Para restringir el uso solo a tu usuario

client = Client(API_KEY, API_SECRET, tld='com')

# Estados de la conversación
SYMBOL, LADO, PCT, PRECIO_INICIO, PERDIDA_MAX, CONFIRMACION = range(6)

# Guardar tareas de monitoreo activas {symbol: Task}
MONITORING_TASKS = {}

# ─── Utilidades Binance ───────────────────────────────────────────────────────

def get_symbol_info(symbol):
    info = client.futures_exchange_info()
    for s in info['symbols']:
        if s['symbol'] == symbol:
            return s
    return None

def get_tick_size(symbol):
    si = get_symbol_info(symbol)
    if si:
        for f in si['filters']:
            if f['filterType'] == 'PRICE_FILTER':
                return f['tickSize']
    return None

def get_step_size(symbol):
    si = get_symbol_info(symbol)
    if si:
        for f in si['filters']:
            if f['filterType'] == 'LOT_SIZE':
                return f['stepSize']
    return None

def _decimals(tick):
    return -Decimal(str(tick)).as_tuple().exponent

def round_price(symbol, price):
    tick = get_tick_size(symbol)
    if not tick:
        return str(price)
    dec = _decimals(tick)
    p = Decimal(str(price))
    t = Decimal(str(tick))
    return format((p // t) * t, f".{dec}f")

def round_quantity(symbol, qty):
    step = get_step_size(symbol)
    if not step:
        return str(qty)
    dec = _decimals(step)
    q = Decimal(str(qty))
    s = Decimal(str(step))
    return format((q // s) * s, f".{dec}f")

def obtener_posiciones(symbol):
    pos_long = pos_short = None
    for pos in client.futures_position_information(symbol=symbol):
        amt = float(pos['positionAmt'])
        entry = float(pos['entryPrice'])
        if amt > 0:
            pos_long = {"cantidad": amt, "precio_entrada": entry}
        elif amt < 0:
            pos_short = {"cantidad": abs(amt), "precio_entrada": entry}
    return pos_long, pos_short

def get_wallet_balance():
    try:
        for asset in client.futures_account()['assets']:
            if asset['asset'] == 'USDT':
                return float(asset['walletBalance'])
    except Exception as e:
        print(f"Error obteniendo balance: {e}")
    return 0.0

def distancia_pct(p1, p2):
    return abs(p1 - p2) / max(p1, p2) * 100

def orden_limite(symbol, side, qty, price, pos_side):
    params = {
        "symbol": symbol,
        "side": side,
        "positionSide": pos_side,
        "type": "LIMIT",
        "quantity": round_quantity(symbol, qty),
        "price": round_price(symbol, price),
        "timeInForce": "GTC",
    }
    return client.futures_create_order(**params)

def orden_market(symbol, side, qty, pos_side):
    params = {
        "symbol": symbol,
        "side": side,
        "positionSide": pos_side,
        "type": "MARKET",
        "quantity": round_quantity(symbol, qty),
    }
    return client.futures_create_order(**params)

def cancelar_orden(symbol, order_id):
    try:
        client.futures_cancel_order(symbol=symbol, orderId=order_id)
    except Exception as e:
        print(f"Aviso cancelando {order_id}: {e}")

def cerrar_todo(symbol):
    client.futures_cancel_all_open_orders(symbol=symbol)
    pos_long, pos_short = obtener_posiciones(symbol)
    if pos_long and pos_long["cantidad"] > 0:
        orden_market(symbol, "SELL", pos_long["cantidad"], "LONG")
    if pos_short and pos_short["cantidad"] > 0:
        orden_market(symbol, "BUY", pos_short["cantidad"], "SHORT")

def calcular_ordenes(start_price, initial_qty, new_dist, direction):
    f = (1 - new_dist / 100) if direction == "down" else (1 + new_dist / 100)
    return [
        {"precio": start_price,        "cantidad": initial_qty},
        {"precio": start_price * f,    "cantidad": initial_qty},
        {"precio": start_price * f**2, "cantidad": initial_qty * 2},
        {"precio": start_price * f**3, "cantidad": initial_qty * 4},
    ]

def calcular_sl(long_qty, short_qty, total_cerrar, ultimo_precio, perdida_max_usd, direction):
    if perdida_max_usd <= 0:
        return None
    if direction == "down":
        net = long_qty - (short_qty - total_cerrar)
        if net <= 0:
            return None
        return ultimo_precio - (perdida_max_usd / net)
    else:
        net = short_qty - (long_qty - total_cerrar)
        if net <= 0:
            return None
        return ultimo_precio + (perdida_max_usd / net)

# ─── Bucle de Monitoreo Asíncrono ────────────────────────────────────────────

async def monitorear_async(context: ContextTypes.DEFAULT_TYPE, symbol, order_ids, new_dist, sl_price, direction, tp_side, tp_pos_side, chat_id):
    filled = {}
    tp_order_id = None
    sl_str = f"{sl_price:.6f}" if sl_price else "N/A"
    
    await context.bot.send_message(chat_id, f"🚀 **Monitoreo Iniciado** [{symbol}]\nSL: `{sl_str}`\nRevisando cada 10s...", parse_mode="Markdown")

    while True:
        await asyncio.sleep(10)
        try:
            current_price = float(client.futures_symbol_ticker(symbol=symbol)["price"])
        except Exception as e:
            print(f"Error precio: {e}")
            continue

        # Verificar SL
        if sl_price:
            sl_hit = (direction == "down" and current_price <= sl_price) or \
                     (direction == "up"   and current_price >= sl_price)
            if sl_hit:
                cerrar_todo(symbol)
                await context.bot.send_message(chat_id, f"🚨 **SL Alcanzado** @ `{current_price:.6f}`. Posiciones cerradas a mercado.")
                return

        # Verificar ejecuciones
        nuevas = False
        for i, oid in enumerate(order_ids):
            if i in filled:
                continue
            try:
                st = client.futures_get_order(symbol=symbol, orderId=oid)
                if st["status"] == "FILLED":
                    fp = float(st["avgPrice"])
                    fq = float(st["executedQty"])
                    filled[i] = {"precio": fp, "cantidad": fq}
                    nuevas = True
                    await context.bot.send_message(chat_id, f"✅ Orden {i+1} ejecutada: `{fq:.4f}` @ `{fp:.6f}`", parse_mode="Markdown")
            except Exception as e:
                print(f"Error orden {oid}: {e}")

        # Verificar TP
        if tp_order_id:
            try:
                tp_st = client.futures_get_order(symbol=symbol, orderId=tp_order_id)
                if tp_st["status"] == "FILLED":
                    fp = float(tp_st["avgPrice"])
                    fq = float(tp_st["executedQty"])
                    await context.bot.send_message(chat_id, f"🎯 **TP Ejecutado**: `{fq:.4f}` @ `{fp:.6f}`", parse_mode="Markdown")
                    
                    pendientes = [order_ids[i] for i in range(len(order_ids)) if i not in filled]
                    for oid in pendientes:
                        cancelar_orden(symbol, oid)
                    
                    await context.bot.send_message(chat_id, "🏁 Ciclo completado con éxito.")
                    return
            except Exception as e:
                print(f"Error TP: {e}")

        # Actualizar TP
        if nuevas:
            total_q = sum(o["cantidad"] for o in filled.values())
            avg_p   = sum(o["precio"] * o["cantidad"] for o in filled.values()) / total_q
            tp_price = avg_p * (1 + new_dist/100) if direction == "down" else avg_p * (1 - new_dist/100)

            if tp_order_id:
                cancelar_orden(symbol, tp_order_id)

            res = orden_limite(symbol, tp_side, total_q, tp_price, tp_pos_side)
            tp_order_id = res["orderId"]
            await context.bot.send_message(chat_id, f"🔄 TP Actualizado: `{total_q:.4f}` @ `{tp_price:.6f}`", parse_mode="Markdown")

        if len(filled) == len(order_ids):
            await context.bot.send_message(chat_id, "ℹ️ Todas las órdenes de cierre ejecutadas. Entrando en fase final de TP/SL...")
            await _fase_final_async(context, symbol, tp_order_id, sl_price, direction, chat_id)
            return

# Fase final
async def _fase_final_async(context, symbol, tp_order_id, sl_price, direction, chat_id):
    while True:
        await asyncio.sleep(10)
        try:
            current_price = float(client.futures_symbol_ticker(symbol=symbol)["price"])
        except Exception:
            continue

        if sl_price:
            sl_hit = (direction == "down" and current_price <= sl_price) or \
                     (direction == "up"   and current_price >= sl_price)
            if sl_hit:
                cerrar_todo(symbol)
                await context.bot.send_message(chat_id, f"🚨 **SL Final alcanzado** @ `{current_price:.6f}`.")
                return

        if tp_order_id:
            try:
                st = client.futures_get_order(symbol=symbol, orderId=tp_order_id)
                if st["status"] == "FILLED":
                    pos_long, pos_short = obtener_posiciones(symbol)
                    if not pos_long and not pos_short:
                        await context.bot.send_message(chat_id, "🎯 **TP Final ejecutado**. Sin posiciones abiertas. Fin.")
                        return
                    tp_order_id = None
            except Exception as e:
                print(f"Error TP final: {e}")

# ─── Flujo de Conversación Telegram ──────────────────────────────────────────

async def auth_filter(update: Update):
    if ALLOWED_CHAT_ID and str(update.effective_chat.id) != str(ALLOWED_CHAT_ID):
        await update.message.reply_text("⛔ No tienes autorización para usar este bot.")
        return False
    return True

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await auth_filter(update): return ConversationHandler.END
    await update.message.reply_text("🤖 **Bot de Cobertura Binance Futures**\n\nEnvía /cobertura para iniciar la gestión.")
    return ConversationHandler.END

async def iniciar_cobertura(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await auth_filter(update): return ConversationHandler.END
    await update.message.reply_text("Ingrese el símbolo del par (ejemplo: `BTCUSDT`):", parse_mode="Markdown")
    return SYMBOL

async def recibir_simbolo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    symbol = update.message.text.strip().upper()
    try:
        pos_long, pos_short = obtener_posiciones(symbol)
        if not pos_long or not pos_short:
            await update.message.reply_text(f"❌ No se detecta cobertura en `{symbol}`. Se requieren posiciones LONG y SHORT abiertas.", parse_mode="Markdown")
            return ConversationHandler.END
        
        context.user_data['symbol'] = symbol
        context.user_data['pos_long'] = pos_long
        context.user_data['pos_short'] = pos_short
        
        keyboard = [
            [InlineKeyboardButton("Cerrar SHORT", callback_data="short")],
            [InlineKeyboardButton("Cerrar LONG", callback_data="long")]
        ]
        
        text = (f"📍 **Posiciones detectadas en {symbol}**:\n"
                f"• LONG: `{pos_long['cantidad']:.4f}` @ `{pos_long['precio_entrada']:.6f}`\n"
                f"• SHORT: `{pos_short['cantidad']:.4f}` @ `{pos_short['precio_entrada']:.6f}`\n\n"
                f"¿Qué posición desea cerrar?")
        
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return LADO
    except Exception as e:
        await update.message.reply_text(f"Error consultando par `{symbol}`: {e}")
        return ConversationHandler.END

async def recibir_lado(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    lado = query.data
    context.user_data['lado'] = lado

    keyboard = [
        [InlineKeyboardButton("5 %", callback_data="5"), InlineKeyboardButton("10 %", callback_data="10")]
    ]
    await query.edit_message_text(f"Seleccionaste cerrar **{lado.upper()}**.\nPorcentaje de monedas a cerrar:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return PCT

async def recibir_pct(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    pct = float(query.data)
    context.user_data['pct'] = pct

    pos_l, pos_s = context.user_data['pos_long'], context.user_data['pos_short']
    hedge_dist = distancia_pct(pos_l["precio_entrada"], pos_s["precio_entrada"])
    mult_distancia = 1.50 if pct == 5 else 1.20
    new_dist = hedge_dist * mult_distancia
    
    context.user_data['hedge_dist'] = hedge_dist
    context.user_data['new_dist'] = new_dist

    await query.edit_message_text(
        f"Distancia cobertura: `{hedge_dist:.2f}%` → Distancia órdenes ({mult_distancia}x): `{new_dist:.2f}%`\n\n"
        f"Ingresa el **Precio de inicio de cierre**:", parse_mode="Markdown"
    )
    return PRECIO_INICIO

async def recibir_precio_inicio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        precio_inicio = float(update.message.text.strip())
        context.user_data['precio_inicio'] = precio_inicio
        
        wallet = get_wallet_balance()
        await update.message.reply_text(
            f"Balance wallet disponible: `{wallet:.2f} USDT`\n\n"
            f"Ingresa la **Pérdida máxima en USD** a arriesgar en SL (ej. 15):", parse_mode="Markdown"
        )
        return PERDIDA_MAX
    except ValueError:
        await update.message.reply_text("⚠️ Precio inválido. Ingresa un número válido:")
        return PRECIO_INICIO

async def recibir_perdida_max(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        perdida_max_usd = float(update.message.text.strip())
        if perdida_max_usd <= 0:
            await update.message.reply_text("El monto debe ser mayor a 0. Reintenta:")
            return PERDIDA_MAX
        
        context.user_data['perdida_max_usd'] = perdida_max_usd

        # Cálculos
        symbol = context.user_data['symbol']
        lado = context.user_data['lado']
        pct = context.user_data['pct']
        precio_inicio = context.user_data['precio_inicio']
        new_dist = context.user_data['new_dist']
        pos_long = context.user_data['pos_long']
        pos_short = context.user_data['pos_short']

        direction = "down" if lado == "short" else "up"
        total_qty = pos_short["cantidad"] if lado == "short" else pos_long["cantidad"]
        init_qty = total_qty * (pct / 100)
        orders = calcular_ordenes(precio_inicio, init_qty, new_dist, direction)
        total_cerrar = sum(o["cantidad"] for o in orders)

        precio_promedio_cierre = sum(o["precio"] * o["cantidad"] for o in orders) / total_cerrar
        sl_price = calcular_sl(pos_long["cantidad"], pos_short["cantidad"], total_cerrar, precio_promedio_cierre, perdida_max_usd, direction)

        context.user_data['orders'] = orders
        context.user_data['direction'] = direction
        context.user_data['sl_price'] = sl_price

        # Resumen
        resumen = f"📝 **RESUMEN DE OPERACIÓN** [{symbol}]\n\n"
        resumen += f"• Posición a cerrar: `{lado.upper()}`\n"
        resumen += f"• Total a cerrar: `{total_cerrar:.4f}` ({pct*8:.1f}% de la posición)\n"
        resumen += f"• Stop Loss: `{f'{sl_price:.6f}' if sl_price else 'N/A'}`\n\n"
        resumen += "**Órdenes Limite:**\n"
        for i, o in enumerate(orders):
            resumen += f"  - Ord {i+1}: `{o['cantidad']:.4f}` @ `{o['precio']:.6f}`\n"

        keyboard = [
            [InlineKeyboardButton("✅ Confirmar y Ejecutar", callback_data="confirmar")],
            [InlineKeyboardButton("❌ Cancelar", callback_data="cancelar")]
        ]

        await update.message.reply_text(resumen, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return CONFIRMACION

    except ValueError:
        await update.message.reply_text("⚠️ Monto inválido. Ingresa un número entero/decimal:")
        return PERDIDA_MAX

async def confirmar_ejecucion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "cancelar":
        await query.edit_message_text("❌ Operación cancelada por el usuario.")
        return ConversationHandler.END

    await query.edit_message_text("⏳ Colocando órdenes en Binance Futures...")

    symbol = context.user_data['symbol']
    lado = context.user_data['lado']
    orders = context.user_data['orders']
    new_dist = context.user_data['new_dist']
    sl_price = context.user_data['sl_price']
    direction = context.user_data['direction']

    close_side     = "BUY"  if lado == "short" else "SELL"
    close_pos_side = "SHORT" if lado == "short" else "LONG"
    tp_side        = "SELL" if lado == "short" else "BUY"
    tp_pos_side    = "LONG" if lado == "short" else "SHORT"

    order_ids = []
    try:
        for o in orders:
            res = orden_limite(symbol, close_side, o["cantidad"], o["precio"], close_pos_side)
            order_ids.append(res["orderId"])

        # Cancelar tarea previa si existe para ese par
        if symbol in MONITORING_TASKS and not MONITORING_TASKS[symbol].done():
            MONITORING_TASKS[symbol].cancel()

        # Iniciar tarea asíncrona de monitoreo
        task = asyncio.create_task(
            monitorear_async(
                context, symbol, order_ids, new_dist,
                sl_price, direction, tp_side, tp_pos_side, query.message.chat_id
            )
        )
        MONITORING_TASKS[symbol] = task

        await context.bot.send_message(query.message.chat_id, "✅ Órdenes colocadas. Monitoreo en segundo plano activado.")

    except Exception as e:
        await context.bot.send_message(query.message.chat_id, f"❌ Error ejecutando órdenes: {e}")

    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Operación cancelada.")
    return ConversationHandler.END

async def cancelar_monitoreo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await auth_filter(update): return
    if not context.args:
        await update.message.reply_text("Uso: `/cancelar_monitoreo SÍMBOLO` (Ej: `/cancelar_monitoreo BTCUSDT`)", parse_mode="Markdown")
        return
    
    symbol = context.args[0].upper()
    if symbol in MONITORING_TASKS and not MONITORING_TASKS[symbol].done():
        MONITORING_TASKS[symbol].cancel()
        await update.message.reply_text(f"🛑 Monitoreo para `{symbol}` detenido.", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"No hay monitoreo activo registrado para `{symbol}`.", parse_mode="Markdown")

# ─── Inicialización de la App Telegram ───────────────────────────────────────

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("cobertura", iniciar_cobertura)],
        states={
            SYMBOL: [MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_simbolo)],
            LADO: [CallbackQueryHandler(recibir_lado)],
            PCT: [CallbackQueryHandler(recibir_pct)],
            PRECIO_INICIO: [MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_precio_inicio)],
            PERDIDA_MAX: [MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_perdida_max)],
            CONFIRMACION: [CallbackQueryHandler(confirmar_ejecucion)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_message=False,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cancelar_monitoreo", cancelar_monitoreo))
    app.add_handler(conv_handler)

    print("Bot activo y listo...")
    app.run_polling()

if __name__ == "__main__":
    main()