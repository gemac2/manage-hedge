import os
import logging
from dotenv import load_dotenv
from binance.client import Client

# Configuración global de logging con timestamps y nivel estructurado
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("hedge_bot")

load_dotenv()

# Variables de Entorno
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY")
BINANCE_SECRET_KEY = os.getenv("BINANCE_SECRET_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Validaciones Críticas de Seguridad
if not TELEGRAM_BOT_TOKEN:
    raise ValueError("CRÍTICO: TELEGRAM_BOT_TOKEN no configurado en .env")
if not TELEGRAM_CHAT_ID:
    raise ValueError("CRÍTICO: TELEGRAM_CHAT_ID no configurado en .env. Configure un ID para restringir acceso.")

# Cliente Central de Binance
binance_client = Client(BINANCE_API_KEY, BINANCE_SECRET_KEY, tld='com')
