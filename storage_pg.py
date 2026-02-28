import os
import logging
from typing import Optional, Dict, Any
import psycopg
from psycopg.types.json import Jsonb

_CONN = None

def get_conn():
    global _CONN
    if _CONN is None or _CONN.closed:
        dsn = os.getenv("DATABASE_URL")
        if not dsn:
            raise RuntimeError("DATABASE_URL is not set in .env")
        _CONN = psycopg.connect(dsn, autocommit=True)
    return _CONN

def safe_db_call(func):
    """Декоратор, който предпазва бота от краш, ако базата падне."""
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            logging.error(f"Postgres error in {func.__name__} (ignored): {e}")
            global _CONN
            if _CONN is not None and getattr(_CONN, 'closed', True):
                _CONN = None # Ресетваме връзката, за да опита пак следващия път
    return wrapper

@safe_db_call
def log_event(event_type: str, symbol: Optional[str] = None, order_id: Optional[str] = None, payload: Optional[Dict[str, Any]] = None):
    payload = payload or {}
    conn = get_conn()
    conn.execute(
        """
        INSERT INTO bot_events (event_type, symbol, order_id, payload)
        VALUES (%s, %s, %s, %s)
        """,
        (event_type, symbol, order_id, Jsonb(payload)),
    )

@safe_db_call
def upsert_order(order_id: str, symbol: str, order: Dict[str, Any]):
    conn = get_conn()
    conn.execute(
        """
        INSERT INTO orders (order_id, symbol, side, type, status, price, amount, cost, filled, average, raw, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
        ON CONFLICT (order_id) DO UPDATE SET
          status = EXCLUDED.status,
          price = EXCLUDED.price,
          amount = EXCLUDED.amount,
          cost = EXCLUDED.cost,
          filled = EXCLUDED.filled,
          average = EXCLUDED.average,
          raw = EXCLUDED.raw,
          updated_at = now()
        """,
        (
            order_id,
            symbol,
            str(order.get("side")) if order.get("side") is not None else None,
            str(order.get("type")) if order.get("type") is not None else None,
            str(order.get("status")) if order.get("status") is not None else None,
            order.get("price"),
            order.get("amount"),
            order.get("cost"),
            order.get("filled"),
            order.get("average"),
            Jsonb(order),
        ),
    )
