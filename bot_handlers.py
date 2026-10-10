import asyncio
from decimal import Decimal
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

from config import ALLOWED_CHAT_ID, logger
from strategy import (
    distancia_pct,
    calcular_ordenes,
    calcular_sl,
    validate_notional,
)
from binance_service import (
    binance_cache,
    async_get_wallet_balance,
    async_get_positions,
    async_get_all_active_positions,
    async_get_ticker_price,
    async_get_order,
    async_create_limit_order,
    async_create_sl_market_order,
    async_cancel_order,
    async_close_all_positions,
)
from database import (
    save_hedge,
    update_hedge_orders,
    mark_hedge_status,
    get_active_hedges,
    get_hedge,
)

# Estados de Conversación
SYMBOL, LADO, PCT, PRECIO_INICIO, PERDIDA_MAX, CONFIRMACION = range(6)

# Tareas en memoria: {symbol: {"task": Task, "orders": [ids], "tp_order_id": int, "sl_order_id": int, "chat_id": int}}
MONITORING_TASKS = {}

# ─── Seguridad y Envío Seguro de Mensajes ────────────────────────────────────

async def auth_filter(update: Update) -> bool:
    chat_id = update.effective_chat.id if update.effective_chat else None
    if not ALLOWED_CHAT_ID:
        logger.error("TELEGRAM_CHAT_ID no configurado en entorno.")
        return False
    if str(chat_id) != str(ALLOWED_CHAT_ID):
        logger.warning(f"Intento no autorizado desde chat_id: {chat_id}")
        if update.callback_query:
            await update.callback_query.answer("⛔ No tienes autorización para usar este bot.", show_alert=True)
        elif update.effective_message:
            await update.effective_message.reply_text("⛔ No tienes autorización para usar este bot.")
        return False
    return True

async def safe_send(bot, chat_id, text: str, **kwargs):
    try:
        return await bot.send_message(chat_id, text, **kwargs)
    except Exception as e:
        logger.warning(f"Error enviando mensaje con parse_mode: {e}. Reintentando texto plano.")
        kwargs.pop("parse_mode", None)
        return await bot.send_message(chat_id, text, **kwargs)

# ─── Bucle de Monitoreo con Stop Loss Nativo en Binance y Persistencia ────────

async def monitorear_async(
    context: ContextTypes.DEFAULT_TYPE,
    symbol: str,
    order_ids: list,
    new_dist: float,
    sl_price: float | None,
    direction: str,
    tp_side: str,
    tp_pos_side: str,
    chat_id: int | str,
    initial_tp_id: int | None = None,
    initial_sl_id: int | None = None
):
    bot = getattr(context, "bot", context)
    filled = {}
    tp_order_id = initial_tp_id
    sl_order_id = initial_sl_id

    # Si direction == "down" (cerrando SHORT), el riesgo de caída es de LONG -> SL vende LONG
    # Si direction == "up" (cerrando LONG), el riesgo de subida es de SHORT -> SL compra SHORT
    sl_side = "SELL" if direction == "down" else "BUY"
    sl_pos_side = "LONG" if direction == "down" else "SHORT"
    sl_str = f"{sl_price:.6f}" if sl_price else "N/A"

    logger.info(f"Iniciando bucle de monitoreo para {symbol} (SL: {sl_str})")

    while True:
        await asyncio.sleep(5)  # Intervalo ágil y optimizado

        try:
            current_price = await async_get_ticker_price(symbol)
        except Exception as e:
            logger.warning(f"Error consultando precio para {symbol}: {e}")
            continue

        # 🛑 PROTECCIÓN 1: Validar si el usuario canceló manualmente las órdenes en Binance
        pendientes_o_llenas = 0
        for oid in order_ids:
            try:
                st = await async_get_order(symbol, oid)
                if st["status"] in ["NEW", "FILLED", "PARTIALLY_FILLED"]:
                    pendientes_o_llenas += 1
            except Exception:
                pass

        if len(filled) == 0 and pendientes_o_llenas == 0:
            await safe_send(
                bot,
                chat_id,
                f"⚠️ **Atención [{symbol}]**: Se detectó que las órdenes fueron canceladas externamente en Binance.\n\n"
                f"🛑 **Monitoreo cancelado automáticamente. Posiciones intactas.**",
                parse_mode="Markdown"
            )
            mark_hedge_status(symbol, "ABORTED")
            if symbol in MONITORING_TASKS:
                del MONITORING_TASKS[symbol]
            return

        # 🛑 PROTECCIÓN 2: Verificar si el Stop Loss Nativo en Binance fue ejecutado
        if sl_order_id:
            try:
                sl_st = await async_get_order(symbol, sl_order_id)
                if sl_st["status"] in ["FILLED", "EXPIRED"]:
                    logger.warning(f"Stop Loss nativo ejecutado en Binance para {symbol}")
                    
                    # Cancelar órdenes pendientes de la grilla
                    pendientes = [order_ids[i] for i in range(len(order_ids)) if i not in filled]
                    for oid in pendientes:
                        await async_cancelar_orden(symbol, oid)
                    if tp_order_id:
                        await async_cancelar_orden(symbol, tp_order_id)

                    # Salir de la posición residual para cerrar el hedge completamente
                    await async_close_all_positions(symbol)
                    mark_hedge_status(symbol, "STOPPED")

                    await safe_send(
                        bot,
                        chat_id,
                        f"🚨 **STOP LOSS EJECUTADO EN BINANCE** [{symbol}]\n\n"
                        f"• Gatillo activado: `{sl_str}`\n"
                        f"• Orden nativa `STOP_MARKET` ejecutada por el motor de Binance.\n"
                        f"• Cobertura cerrada y riesgo detenido según lo planificado.",
                        parse_mode="Markdown"
                    )
                    if symbol in MONITORING_TASKS:
                        del MONITORING_TASKS[symbol]
                    return
            except Exception as e:
                logger.error(f"Error verificando estado de SL {sl_order_id}: {e}")

        # ── Verificar estado de órdenes de la grilla
        nuevas = False
        for i, oid in enumerate(order_ids):
            if i in filled:
                continue
            try:
                st = await async_get_order(symbol, oid)
                if st["status"] == "FILLED":
                    fp = float(st["avgPrice"])
                    fq = float(st["executedQty"])
                    filled[i] = {"precio": fp, "cantidad": fq}
                    nuevas = True
                    await safe_send(
                        bot,
                        chat_id,
                        f"✅ Orden {i+1} ejecutada: `{fq:.4f}` @ `{fp:.6f}`",
                        parse_mode="Markdown"
                    )
            except Exception as e:
                logger.error(f"Error consultando orden {oid}: {e}")

        # ── Verificar si el Take Profit (TP) fue ejecutado
        if tp_order_id:
            try:
                tp_st = await async_get_order(symbol, tp_order_id)
                if tp_st["status"] == "FILLED":
                    fp = float(tp_st["avgPrice"])
                    fq = float(tp_st["executedQty"])
                    await safe_send(
                        bot,
                        chat_id,
                        f"🎯 **TP Ejecutado**: `{fq:.4f}` @ `{fp:.6f}`",
                        parse_mode="Markdown"
                    )

                    # Cancelar órdenes pendientes de la grilla
                    pendientes = [order_ids[i] for i in range(len(order_ids)) if i not in filled]
                    for oid in pendientes:
                        await async_cancelar_orden(symbol, oid)

                    # Cancelar el Stop Loss nativo en Binance
                    if sl_order_id:
                        await async_cancelar_orden(symbol, sl_order_id)

                    mark_hedge_status(symbol, "COMPLETED")
                    await safe_send(
                        bot,
                        chat_id,
                        "🏁 **Ciclo completado con éxito.** Stop Loss cancelado en Binance."
                    )
                    if symbol in MONITORING_TASKS:
                        del MONITORING_TASKS[symbol]
                    return
            except Exception as e:
                logger.error(f"Error consultando TP {tp_order_id}: {e}")

        # ── ACCIÓN INMEDIATA TRAS EJECUCIÓN DE ÓRDENES ─────────────────────────
        if nuevas:
            # 🛡️ REGLA PRINCIPAL: Colocar STOP LOSS Nativo en Binance apenas toque la 1ra orden
            if sl_price and sl_order_id is None:
                try:
                    logger.info(f"Colocando STOP_MARKET nativo en Binance para {symbol} @ {sl_price}")
                    sl_res = await async_create_sl_market_order(symbol, sl_side, sl_pos_side, sl_price)
                    sl_order_id = sl_res["orderId"]
                    update_hedge_orders(symbol, sl_order_id=sl_order_id)
                    if symbol in MONITORING_TASKS:
                        MONITORING_TASKS[symbol]["sl_order_id"] = sl_order_id

                    await safe_send(
                        bot,
                        chat_id,
                        f"🛡️ **STOP LOSS NATIVO ARMADO EN BINANCE**\n"
                        f"• Símbolo: `{symbol}`\n"
                        f"• Posición protegida: `{sl_pos_side}`\n"
                        f"• Tipo: `STOP_MARKET` ({sl_side})\n"
                        f"• Gatillo: `{sl_price:.6f}`\n"
                        f"• *Si la volatilidad sacude el mercado, Binance ejecuta el cierre sin latencia.*",
                        parse_mode="Markdown"
                    )
                except Exception as e:
                    logger.critical(f"Error al colocar Stop Loss nativo en Binance: {e}")
                    await safe_send(
                        bot,
                        chat_id,
                        f"⚠️ **Error al armar SL nativo en Binance**: {e}\nSe mantendrá intento en la siguiente ronda."
                    )

            # ── Recalcular y colocar TP promedio en Binance
            total_q = sum(o["cantidad"] for o in filled.values())
            avg_p   = sum(o["precio"] * o["cantidad"] for o in filled.values()) / total_q
            tp_price = avg_p * (1 + new_dist/100) if direction == "down" else avg_p * (1 - new_dist/100)

            if tp_order_id:
                await async_cancelar_orden(symbol, tp_order_id)

            try:
                res = await async_create_limit_order(symbol, tp_side, total_q, tp_price, tp_pos_side)
                tp_order_id = res["orderId"]
                update_hedge_orders(symbol, tp_order_id=tp_order_id)
                if symbol in MONITORING_TASKS:
                    MONITORING_TASKS[symbol]["tp_order_id"] = tp_order_id

                await safe_send(
                    bot,
                    chat_id,
                    f"🔄 **TP Actualizado en Binance**: `{total_q:.4f}` @ `{tp_price:.6f}`",
                    parse_mode="Markdown"
                )
            except Exception as e:
                logger.error(f"Error actualizando TP para {symbol}: {e}")
                await safe_send(bot, chat_id, f"⚠️ Error actualizando TP: {e}")

# ─── Comandos Informativos y de Utilidad ─────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await auth_filter(update): return ConversationHandler.END
    msg = (
        "🤖 **Bot de Cobertura Binance Futures v2.0**\n\n"
        "Comandos disponibles:\n"
        "• `/cobertura` - Iniciar desmonte de cobertura asistido\n"
        "• `/status` - Ver estado de coberturas y órdenes activas\n"
        "• `/posiciones` - Ver posiciones abiertas en Binance Futures\n"
        "• `/abortar SÍMBOLO` - Cancelar cobertura y órdenes específicas\n"
        "• `/help` - Explicación detallada del funcionamiento\n"
        "• `/cancel` - Cancelar menú interactivo actual"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")
    return ConversationHandler.END

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await auth_filter(update): return
    msg = (
        "📖 **Manual de Operación y Protección**\n\n"
        "1. **Desmonte de Cobertura (`/cobertura`)**:\n"
        "   - Selecciona si deseas cerrar la posición SHORT o LONG.\n"
        "   - Define el porcentaje inicial (5% o 10%).\n"
        "   - Ingresa el precio de inicio y la pérdida máxima en USD.\n\n"
        "2. **Protección de Stop Loss Nativo**:\n"
        "   - El Stop Loss **NO** se coloca de inmediato para permitir que el hedge permanezca delta-neutral.\n"
        "   - **Apenas toca la primera orden**, el bot coloca instantáneamente una orden `STOP_MARKET` directamente en los servidores de Binance.\n"
        "   - Si hay un salto repentino de volatilidad, Binance ejecuta el cierre en microsegundos.\n\n"
        "3. **Persistencia contra caídas**:\n"
        "   - El estado se almacena en SQLite. Si el bot se reinicia, reanuda automáticamente el monitoreo sin duplicar órdenes."
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await auth_filter(update): return
    active_in_db = get_active_hedges()

    if not active_in_db and not MONITORING_TASKS:
        await update.message.reply_text("ℹ️ No hay coberturas activas bajo monitoreo en este momento.")
        return

    report = "📊 **ESTADO DE COBERTURAS ACTIVAS**\n\n"
    for h in active_in_db:
        sym = h['symbol']
        sl_str = f"{h['sl_price']:.6f}" if h['sl_price'] else "N/A"
        sl_activo = "✅ Colocado en Binance" if h['sl_order_id'] else "⏳ En espera de 1ra orden"
        tp_activo = f"✅ Orden #{h['tp_order_id']}" if h['tp_order_id'] else "⏳ En espera"

        report += (
            f"🔹 **{sym}**\n"
            f"• Lado: Cierre de `{h['lado'].upper()}`\n"
            f"• Órdenes grilla: `{len(h['order_ids'])}` órdenes\n"
            f"• Stop Loss: `{sl_str}` ({sl_activo})\n"
            f"• Take Profit: {tp_activo}\n"
            f"• Cancelar: `/abortar {sym}`\n\n"
        )

    await update.message.reply_text(report, parse_mode="Markdown")

async def posiciones_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await auth_filter(update): return
    await update.message.reply_text("Consultando posiciones abiertas en Binance Futures...")
    positions = await async_get_all_active_positions()

    if not positions:
        await update.message.reply_text("ℹ️ No tienes posiciones abiertas en Binance Futures.")
        return

    msg = "📈 **POSICIONES ABIERTAS EN BINANCE FUTURES**\n\n"
    for p in positions:
        pnl = p['unrealizedProfit']
        emoji_pnl = "🟢" if pnl >= 0 else "🔴"
        msg += (
            f"• **{p['symbol']}** ({p['side']} {p['leverage']}x)\n"
            f"  Cantidad: `{p['amount']}` | Entrada: `{p['entryPrice']:.4f}`\n"
            f"  Mark: `{p['markPrice']:.4f}` | PnL: {emoji_pnl} `{pnl:+.2f} USDT`\n\n"
        )

    await update.message.reply_text(msg, parse_mode="Markdown")

# ─── Flujo de Conversación de Cobertura ──────────────────────────────────────

async def iniciar_cobertura(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await auth_filter(update): return ConversationHandler.END
    await update.message.reply_text("Ingrese el símbolo del par (ejemplo: `BTCUSDT`):", parse_mode="Markdown")
    return SYMBOL

async def recibir_simbolo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    symbol = update.message.text.strip().upper()
    try:
        binance_cache.get_filters(symbol)
        pos_long, pos_short = await async_get_positions(symbol)
        if not pos_long or not pos_short:
            await update.message.reply_text(
                f"❌ No se detecta cobertura en `{symbol}`. Se requieren posiciones LONG y SHORT abiertas simultáneamente.",
                parse_mode="Markdown"
            )
            return ConversationHandler.END

        context.user_data['symbol'] = symbol
        context.user_data['pos_long'] = pos_long
        context.user_data['pos_short'] = pos_short

        keyboard = [
            [InlineKeyboardButton("Cerrar SHORT", callback_data="short")],
            [InlineKeyboardButton("Cerrar LONG", callback_data="long")]
        ]

        text = (
            f"📍 **Posiciones detectadas en {symbol}**:\n"
            f"• LONG: `{pos_long['cantidad']:.4f}` @ `{pos_long['precio_entrada']:.6f}`\n"
            f"• SHORT: `{pos_short['cantidad']:.4f}` @ `{pos_short['precio_entrada']:.6f}`\n\n"
            f"¿Qué posición desea cerrar?"
        )

        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return LADO
    except Exception as e:
        logger.error(f"Error consultando par {symbol}: {e}")
        await update.message.reply_text(f"Error consultando par `{symbol}`: {e}")
        return ConversationHandler.END

async def recibir_lado(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await auth_filter(update): return ConversationHandler.END
    query = update.callback_query
    await query.answer()
    lado = query.data
    context.user_data['lado'] = lado

    keyboard = [
        [InlineKeyboardButton("5 %", callback_data="5"), InlineKeyboardButton("10 %", callback_data="10")]
    ]
    await query.edit_message_text(
        f"Seleccionaste cerrar **{lado.upper()}**.\nPorcentaje de monedas a cerrar:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )
    return PCT

async def recibir_pct(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await auth_filter(update): return ConversationHandler.END
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
        f"Ingresa el **Precio de inicio de cierre**:",
        parse_mode="Markdown"
    )
    return PRECIO_INICIO

async def recibir_precio_inicio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        precio_inicio = float(update.message.text.strip())
        context.user_data['precio_inicio'] = precio_inicio

        wallet = await async_get_wallet_balance()
        await update.message.reply_text(
            f"Balance wallet disponible: `{wallet:.2f} USDT`\n\n"
            f"Ingresa la **Pérdida máxima en USD** a arriesgar en SL (ej. 15):",
            parse_mode="Markdown"
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

        # Validación del filtro MIN_NOTIONAL de Binance
        filters_sym = binance_cache.get_filters(symbol)
        min_notional = filters_sym.get("minNotional", Decimal("5.0")) if filters_sym else Decimal("5.0")
        for idx, o in enumerate(orders):
            if not validate_notional(o["cantidad"], o["precio"], min_notional):
                await update.message.reply_text(
                    f"⚠️ La orden #{idx+1} tiene un valor inferior al mínimo de Binance ({min_notional} USDT). "
                    f"Aumenta el porcentaje de cierre o el balance de cobertura.",
                    parse_mode="Markdown"
                )
                return PERDIDA_MAX

        precio_promedio_cierre = sum(o["precio"] * o["cantidad"] for o in orders) / total_cerrar
        sl_price = calcular_sl(pos_long["cantidad"], pos_short["cantidad"], total_cerrar, precio_promedio_cierre, perdida_max_usd, direction)

        context.user_data['orders'] = orders
        context.user_data['direction'] = direction
        context.user_data['sl_price'] = sl_price

        resumen = f"📝 **RESUMEN DE OPERACIÓN** [{symbol}]\n\n"
        resumen += f"• Posición a cerrar: `{lado.upper()}`\n"
        resumen += f"• Total a cerrar: `{total_cerrar:.4f}` ({pct*8:.1f}% de la posición)\n"
        resumen += f"• Stop Loss Nativo: `{f'{sl_price:.6f}' if sl_price else 'N/A'}`\n"
        resumen += f"  *(Se colocará en Binance automáticamente apenas toque la 1ra orden)*\n\n"
        resumen += "**Órdenes Límite (Grilla):**\n"
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
    if not await auth_filter(update): return ConversationHandler.END
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
            res = await async_create_limit_order(symbol, close_side, o["cantidad"], o["precio"], close_pos_side)
            order_ids.append(res["orderId"])

        # Cancelar tarea previa si existía en memoria
        if symbol in MONITORING_TASKS:
            MONITORING_TASKS[symbol]["task"].cancel()

        # Guardar estado de cobertura en SQLite para persistencia ante reinicios
        save_hedge(
            symbol=symbol,
            chat_id=query.message.chat_id,
            lado=lado,
            direction=direction,
            new_dist=new_dist,
            sl_price=sl_price,
            tp_side=tp_side,
            tp_pos_side=tp_pos_side,
            order_ids=order_ids
        )

        # Iniciar tarea asíncrona de monitoreo
        task = asyncio.create_task(
            monitorear_async(
                context, symbol, order_ids, new_dist,
                sl_price, direction, tp_side, tp_pos_side, query.message.chat_id
            )
        )
        MONITORING_TASKS[symbol] = {
            "task": task,
            "orders": order_ids,
            "tp_order_id": None,
            "sl_order_id": None,
            "chat_id": query.message.chat_id
        }

        await safe_send(
            context.bot,
            query.message.chat_id,
            f"✅ **Órdenes colocadas y monitoreo activo.**\n"
            f"El Stop Loss nativo quedará armado en Binance en cuanto se toque la primera orden.\n"
            f"Para ver el estado usa `/status` o para detener `/abortar {symbol}`",
            parse_mode="Markdown"
        )

    except Exception as e:
        logger.error(f"Error ejecutando órdenes para {symbol}: {e}")
        await safe_send(context.bot, query.message.chat_id, f"❌ Error ejecutando órdenes: {e}")

    return ConversationHandler.END

async def abortar_cobertura(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await auth_filter(update): return
    if not context.args:
        await update.message.reply_text("Uso: `/abortar SÍMBOLO` (Ej: `/abortar BTCUSDT`)", parse_mode="Markdown")
        return

    symbol = context.args[0].upper()

    # Cancelar tarea en memoria si está activa
    if symbol in MONITORING_TASKS:
        MONITORING_TASKS[symbol]["task"].cancel()
        del MONITORING_TASKS[symbol]

    # Obtener registro de persistencia
    hedge_data = get_hedge(symbol)
    canceladas = 0

    if hedge_data:
        # Cancelar únicamente las órdenes específicas creadas para esta cobertura
        for oid in hedge_data.get("order_ids", []):
            res = await async_cancel_order(symbol, oid)
            if res: canceladas += 1
        if hedge_data.get("tp_order_id"):
            res = await async_cancel_order(symbol, hedge_data["tp_order_id"])
            if res: canceladas += 1
        if hedge_data.get("sl_order_id"):
            res = await async_cancel_order(symbol, hedge_data["sl_order_id"])
            if res: canceladas += 1

        mark_hedge_status(symbol, "ABORTED")
        await update.message.reply_text(
            f"🛑 **Monitoreo detenido y {canceladas} órdenes de cobertura eliminadas en Binance** para `{symbol}`.\n"
            f"Tus posiciones quedan intactas.",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            f"⚠️ No se encontró registro de cobertura activa para `{symbol}`.",
            parse_mode="Markdown"
        )

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    symbol = context.user_data.get('symbol')
    if symbol and symbol in MONITORING_TASKS:
        await abortar_cobertura(update, context)
        return ConversationHandler.END

    await update.message.reply_text("Operación o menú interactivo cancelado.")
    return ConversationHandler.END
