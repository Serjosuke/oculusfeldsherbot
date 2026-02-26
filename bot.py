import os
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ConversationHandler,
    ContextTypes, filters
)

from db import init_db, upsert_appointment, get_appointment

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("tg-appointments")

BOT_TOKEN = os.getenv("BOT_TOKEN")
TZ = ZoneInfo("Asia/Yakutsk")

ASK_FIO, ASK_TIME = range(2)

def _parse_dt(text: str) -> datetime | None:
    """
    Accepts:
      - "2026-02-25 14:30"
      - "2026-02-25 14:30:00"
      - "25.02.2026 14:30"
    Returns timezone-aware datetime in Asia/Yakutsk.
    """
    text = text.strip()
    fmts = [
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%d.%m.%Y %H:%M",
        "%d.%m.%Y %H:%M:%S",
    ]
    for f in fmts:
        try:
            dt_naive = datetime.strptime(text, f)
            return dt_naive.replace(tzinfo=TZ)
        except ValueError:
            pass
    return None

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Я помогу записаться к офтальмологу.\n\n"
        "Команды:\n"
        "/book — записаться\n"
        "/my — посмотреть мою запись\n"
        "/cancel — отменить ввод"
    )

async def my(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    row = get_appointment(user.id)
    if not row:
        await update.message.reply_text("У тебя пока нет записи. Напиши /book чтобы записаться.")
        return
    # row["appointment"] уже datetime (обычно), но зависит от драйвера; приводим аккуратно
    appt = row["appointment"]
    if isinstance(appt, str):
        appt_str = appt
    else:
        appt_local = appt.astimezone(TZ)
        appt_str = appt_local.strftime("%d.%m.%Y %H:%M")
    await update.message.reply_text(f"Твоя запись:\nФИО: {row['fio']}\nВремя: {appt_str}")

async def book(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Напиши, пожалуйста, ФИО (например: Иванов Иван Иванович).",
        reply_markup=ReplyKeyboardRemove()
    )
    return ASK_FIO

async def ask_fio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    fio = update.message.text.strip()
    if len(fio) < 5:
        await update.message.reply_text("ФИО слишком короткое. Попробуй ещё раз.")
        return ASK_FIO

    context.user_data["fio"] = fio

    kb = ReplyKeyboardMarkup(
        [["2026-02-25 14:30", "2026-02-25 15:00"], ["/cancel"]],
        resize_keyboard=True
    )

    await update.message.reply_text(
        "Теперь отправь дату и время приёма.\n"
        "Формат: `YYYY-MM-DD HH:MM` или `DD.MM.YYYY HH:MM`\n"
        "Например: 2026-02-25 14:30",
        reply_markup=kb,
        parse_mode="Markdown"
    )
    return ASK_TIME

async def ask_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    dt = _parse_dt(text)
    if not dt:
        await update.message.reply_text(
            "Не понял дату/время 😅\n"
            "Напиши, например: 2026-02-25 14:30 или 25.02.2026 14:30"
        )
        return ASK_TIME

    # можно запретить прошлое
    now = datetime.now(TZ)
    if dt < now:
        await update.message.reply_text("Это время уже в прошлом. Введи, пожалуйста, будущее время.")
        return ASK_TIME

    user = update.effective_user
    fio = context.user_data["fio"]

    # сохраним в БД (ISO с таймзоной)
    upsert_appointment(user.id, fio, dt.isoformat())

    await update.message.reply_text(
        f"Готово ✅\nЗаписал(а):\nФИО: {fio}\nВремя: {dt.strftime('%d.%m.%Y %H:%M')} (Yakutsk)",
        reply_markup=ReplyKeyboardRemove()
    )
    context.user_data.clear()
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Ок, отменил ввод.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set")

    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("book", book)],
        states={
            ASK_FIO: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_fio)],
            ASK_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_time)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("my", my))
    app.add_handler(conv)
    app.add_handler(CommandHandler("cancel", cancel))

    logger.info("Bot started (polling)...")
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
