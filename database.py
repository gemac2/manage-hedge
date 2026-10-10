import sqlite3
import json
from datetime import datetime, timezone
from config import logger

DB_FILE = "hedge_state.db"

def get_connection():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """Inicializa la tabla de coberturas si no existe."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS active_hedges (
                symbol TEXT PRIMARY KEY,
                chat_id INTEGER NOT NULL,
                lado TEXT NOT NULL,
                direction TEXT NOT NULL,
                new_dist REAL NOT NULL,
                sl_price REAL,
                tp_side TEXT NOT NULL,
                tp_pos_side TEXT NOT NULL,
                order_ids TEXT NOT NULL,
                tp_order_id INTEGER,
                sl_order_id INTEGER,
                status TEXT NOT NULL DEFAULT 'ACTIVE',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.commit()
    logger.info("Base de datos SQLite de persistencia inicializada correctamente.")

def save_hedge(symbol: str, chat_id: int, lado: str, direction: str, new_dist: float,
               sl_price: float | None, tp_side: str, tp_pos_side: str, order_ids: list):
    """Guarda o actualiza una cobertura activa."""
    now = datetime.now(timezone.utc).isoformat()
    order_ids_json = json.dumps(order_ids)
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO active_hedges (
                symbol, chat_id, lado, direction, new_dist, sl_price,
                tp_side, tp_pos_side, order_ids, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
                chat_id=excluded.chat_id,
                lado=excluded.lado,
                direction=excluded.direction,
                new_dist=excluded.new_dist,
                sl_price=excluded.sl_price,
                tp_side=excluded.tp_side,
                tp_pos_side=excluded.tp_pos_side,
                order_ids=excluded.order_ids,
                status='ACTIVE',
                updated_at=excluded.updated_at
        """, (symbol, chat_id, lado, direction, new_dist, sl_price, tp_side, tp_pos_side, order_ids_json, now, now))
        conn.commit()

def update_hedge_orders(symbol: str, tp_order_id: int | None = None, sl_order_id: int | None = None):
    """Actualiza los IDs de órdenes de TP o SL nativo en Binance."""
    now = datetime.now(timezone.utc).isoformat()
    with get_connection() as conn:
        cursor = conn.cursor()
        if tp_order_id is not None and sl_order_id is not None:
            cursor.execute("""
                UPDATE active_hedges SET tp_order_id=?, sl_order_id=?, updated_at=? WHERE symbol=?
            """, (tp_order_id, sl_order_id, now, symbol))
        elif tp_order_id is not None:
            cursor.execute("""
                UPDATE active_hedges SET tp_order_id=?, updated_at=? WHERE symbol=?
            """, (tp_order_id, now, symbol))
        elif sl_order_id is not None:
            cursor.execute("""
                UPDATE active_hedges SET sl_order_id=?, updated_at=? WHERE symbol=?
            """, (sl_order_id, now, symbol))
        conn.commit()

def mark_hedge_status(symbol: str, status: str):
    """Actualiza el estado de la cobertura ('COMPLETED', 'STOPPED', 'ABORTED')."""
    now = datetime.now(timezone.utc).isoformat()
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE active_hedges SET status=?, updated_at=? WHERE symbol=?
        """, (status, now, symbol))
        conn.commit()

def get_active_hedges() -> list[dict]:
    """Recupera todas las coberturas con estado 'ACTIVE' para restaurarlas tras reinicio."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM active_hedges WHERE status='ACTIVE'")
        rows = cursor.fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d['order_ids'] = json.loads(d['order_ids'])
            result.append(d)
        return result

def get_hedge(symbol: str) -> dict | None:
    """Recupera la información de cobertura para un símbolo específico."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM active_hedges WHERE symbol=?", (symbol,))
        row = cursor.fetchone()
        if row:
            d = dict(row)
            d['order_ids'] = json.loads(d['order_ids'])
            return d
        return None
