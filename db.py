import os
import psycopg
from psycopg.rows import dict_row

DATABASE_URL = os.getenv("DATABASE_URL")

def get_conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set")
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)

def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS appointments (
                  id BIGSERIAL PRIMARY KEY,
                  tg_id BIGINT UNIQUE NOT NULL,
                  fio TEXT NOT NULL,
                  appointment TIMESTAMPTZ NOT NULL
                );
            """)
            conn.commit()

def upsert_appointment(tg_id: int, fio: str, appointment_iso: str):
    """
    appointment_iso: ISO string with timezone, e.g. 2026-02-25T14:30:00+01:00
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO appointments (tg_id, fio, appointment)
                VALUES (%s, %s, %s)
                ON CONFLICT (tg_id)
                DO UPDATE SET
                  fio = EXCLUDED.fio,
                  appointment = EXCLUDED.appointment;
            """, (tg_id, fio, appointment_iso))
            conn.commit()

def get_appointment(tg_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, tg_id, fio, appointment FROM appointments WHERE tg_id=%s", (tg_id,))
            return cur.fetchone()
