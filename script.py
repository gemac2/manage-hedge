import signal
import asyncio
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    filters,
)

from config import TELEGRAM_BOT_TOKEN, logger
from database import init_db, get_active_hedges
from binance_service import binance_cache
from bot_handlers import (
    start,
    help_command,
    status_command,
    posiciones_command,
    abortar_cobertura,
    cancel,
    iniciar_cobertura,
    recibir_simbolo,
    recibir_lado,
    recibir_pct,
    recibir_precio_inicio,
    recibir_perdida_max,
    confirmar_ejecucion,
    monitorear_async,
    safe_send,
    MONITORING_TASKS,
    SYMBOL,
    LADO,
    PCT,
    PRECIO_INICIO,
    PERDIDA_MAX,
    CONFIRMACION,
)

async def post_init(application: Application):
    """
    Se ejecuta al iniciar el event loop.
    Restaura y reconecta automáticamente las coberturas activas desde SQLite tras un reinicio de Railway/VPS.
    """
    logger.info("Verificando coberturas activas en SQLite...")
    active_hedges = get_active_hedges()
    if not active_hedges:
        logger.info("No hay coberturas pendientes de restauración.")
        return

    logger.info(f"Reactivando {len(active_hedges)} coberturas activas desde SQLite...")
    for h in active_hedges:
        sym = h['symbol']
        task = asyncio.create_task(
            monitorear_async(
                context=application,
                symbol=sym,
                order_ids=h['order_ids'],
                new_dist=h['new_dist'],
                sl_price=h['sl_price'],
                direction=h['direction'],
                tp_side=h['tp_side'],
                tp_pos_side=h['tp_pos_side'],
                chat_id=h['chat_id'],
                initial_tp_id=h['tp_order_id'],
                initial_sl_id=h['sl_order_id']
            )
        )
        MONITORING_TASKS[sym] = {
            "task": task,
            "orders": h['order_ids'],
            "tp_order_id": h['tp_order_id'],
            "sl_order_id": h['sl_order_id'],
            "chat_id": h['chat_id']
        }
        await safe_send(
            application.bot,
            h['chat_id'],
            f"🔄 **Servidor Reiniciado**: Monitoreo reconectado automáticamente para `{sym}`.\n"
            f"Tus órdenes y Stop Loss siguen protegidos.",
            parse_mode="Markdown"
        )

def main():
    logger.info("Iniciando Bot de Cobertura Binance Futures v2.0...")

    # 1. Inicializar almacenamiento SQLite
    init_db()

    # 2. Pre-cargar filtros de símbolos en memoria
    binance_cache.refresh()

    # 3. Construir aplicación de Telegram con hook post_init para reconexión resiliente
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()

    # 4. Flujo interactivo de cobertura
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
        fallbacks=[
            CommandHandler("cancel", cancel),
            CommandHandler("abortar", abortar_cobertura)
        ],
        per_message=False,
    )

    # 5. Registrar Handlers y Comandos
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("posiciones", posiciones_command))
    app.add_handler(CommandHandler("abortar", abortar_cobertura))
    app.add_handler(conv_handler)

    logger.info("Bot listo y escuchando en Telegram.")

    # 6. Ejecutar polling con manejo seguro de señales de terminación
    app.run_polling(
        drop_pending_updates=True,
        close_loop=True,
        stop_signals=(signal.SIGTERM, signal.SIGINT, signal.SIGABRT)
    )

if __name__ == "__main__":
    main()