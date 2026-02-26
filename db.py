import os
import psycopg
from psycopg.rows import dict_row

DATABASE_URL = os.getenv("DATABASE_URL")


def get_conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set")
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def init_db():
    """
    Creates helper table for Telegram linking and migrates appointments table
    to support patient_id.
    Assumes основной проект уже создал таблицу patients (id, fio, passport, birth_date, ...).
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            # 1) Таблица привязок Telegram -> Patient/User
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS telegram_identities (
                  tg_id BIGINT PRIMARY KEY,
                  patient_id BIGINT NULL,
                  user_id BIGINT NULL,
                  telegram_username TEXT NULL,
                  verified_at TIMESTAMPTZ NULL,
                  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                  CONSTRAINT telegram_identities_one_identity CHECK (
                    (patient_id IS NOT NULL AND user_id IS NULL) OR
                    (patient_id IS NULL AND user_id IS NOT NULL)
                  )
                );
                """
            )

            # 2) Таблица записей (если была старая — расширяем)
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS appointments (
                  id BIGSERIAL PRIMARY KEY,
                  tg_id BIGINT UNIQUE NOT NULL,
                  fio TEXT NOT NULL,
                  appointment TIMESTAMPTZ NOT NULL
                );
                """
            )

            # Миграция: добавляем patient_id и служебные поля
            cur.execute("ALTER TABLE appointments ADD COLUMN IF NOT EXISTS patient_id BIGINT NULL;")
            cur.execute("ALTER TABLE appointments ADD COLUMN IF NOT EXISTS created_by_tg_id BIGINT NULL;")
            cur.execute(
                "ALTER TABLE appointments ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();"
            )

            # Уникальность по patient_id (чтобы у пациента была 1 активная запись как сейчас у tg_id)
            cur.execute(
                """
                DO $$
                BEGIN
                  IF NOT EXISTS (
                    SELECT 1 FROM pg_indexes WHERE indexname = 'appointments_patient_id_uq'
                  ) THEN
                    CREATE UNIQUE INDEX appointments_patient_id_uq
                      ON appointments (patient_id)
                      WHERE patient_id IS NOT NULL;
                  END IF;
                END $$;
                """
            )

            conn.commit()


def get_identity(tg_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT tg_id, patient_id, user_id, telegram_username, verified_at
                FROM telegram_identities
                WHERE tg_id=%s
                """,
                (tg_id,),
            )
            return cur.fetchone()


def link_patient_by_passport_and_birthdate(
    tg_id: int, telegram_username: str | None, passport: str, birth_date_iso: str
):
    """
    Ищем пациента в основной таблице patients и привязываем tg_id -> patient_id.
    birth_date_iso: 'YYYY-MM-DD'
    Возвращает dict пациента (id, fio) или None если не найден.
    """
    passport = passport.strip()

    with get_conn() as conn:
        with conn.cursor() as cur:
            # ВАЖНО: предполагаем таблица patients уже существует в основной БД
            cur.execute(
                """
                SELECT id, fio
                FROM patients
                WHERE passport = %s AND birth_date = %s
                LIMIT 1
                """,
                (passport, birth_date_iso),
            )
            patient = cur.fetchone()
            if not patient:
                return None

            cur.execute(
                """
                INSERT INTO telegram_identities (tg_id, patient_id, telegram_username, verified_at)
                VALUES (%s, %s, %s, now())
                ON CONFLICT (tg_id)
                DO UPDATE SET
                  patient_id = EXCLUDED.patient_id,
                  user_id = NULL,
                  telegram_username = EXCLUDED.telegram_username,
                  verified_at = now()
                """,
                (tg_id, patient["id"], telegram_username),
            )
            conn.commit()
            return patient


def upsert_appointment_for_patient(patient_id: int, tg_id: int, fio: str, appointment_iso: str):
    """
    Создаёт/обновляет запись пациента (одна активная запись на patient_id).
    Пишем fio снапшотом (чтобы /my не зависел от join, но это опционально).
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO appointments (patient_id, tg_id, fio, appointment, created_by_tg_id, updated_at)
                VALUES (%s, %s, %s, %s, %s, now())
                ON CONFLICT (patient_id)
                DO UPDATE SET
                  tg_id = EXCLUDED.tg_id,
                  fio = EXCLUDED.fio,
                  appointment = EXCLUDED.appointment,
                  created_by_tg_id = EXCLUDED.created_by_tg_id,
                  updated_at = now()
                """,
                (patient_id, tg_id, fio, appointment_iso, tg_id),
            )
            conn.commit()


def get_my_appointment(tg_id: int):
    """
    Возвращает appointment, fio, patient_id для текущего telegram пользователя.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT a.id, a.patient_id, a.tg_id, a.fio, a.appointment, a.updated_at
                FROM appointments a
                JOIN telegram_identities ti ON ti.patient_id = a.patient_id
                WHERE ti.tg_id = %s
                LIMIT 1
                """,
                (tg_id,),
            )
            return cur.fetchone()
        
def upsert_appointment(tg_id: int, fio: str, appointment_iso: str):
    """
    Backward-compatible: создаёт/обновляет запись по tg_id (как было раньше).
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO appointments (tg_id, fio, appointment, updated_at)
                VALUES (%s, %s, %s, now())
                ON CONFLICT (tg_id)
                DO UPDATE SET
                  fio = EXCLUDED.fio,
                  appointment = EXCLUDED.appointment,
                  updated_at = now()
                """,
                (tg_id, fio, appointment_iso),
            )
            conn.commit()


def get_appointment(tg_id: int):
    """
    Backward-compatible: достаёт запись по tg_id.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, patient_id, tg_id, fio, appointment, updated_at
                FROM appointments
                WHERE tg_id = %s
                LIMIT 1
                """,
                (tg_id,),
            )
            return cur.fetchone()
