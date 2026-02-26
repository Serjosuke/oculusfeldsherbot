import os
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram import Update, ReplyKeyboardRemove, BotCommand, MenuButtonCommands
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ConversationHandler,
    ContextTypes, filters
)

from db import (
    init_db,
    get_identity,
    link_patient_by_passport_and_birthdate,
    upsert_appointment_for_patient,
    get_my_appointment,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("tg-appointments")

BOT_TOKEN = os.getenv("BOT_TOKEN")
TZ = ZoneInfo("Asia/Yakutsk")

ASK_PASSPORT, ASK_BDATE, ASK_TIME = range(3)


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


def _parse_birth_date(text: str) -> str | None:
    """
    Accepts:
      - "DD.MM.YYYY"
      - "YYYY-MM-DD"
    Returns ISO date 'YYYY-MM-DD' or None.
    """
    text = text.strip()
    for f in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            d = datetime.strptime(text, f).date()
            return d.isoformat()
        except ValueError:
            pass
    return None


async def post_init(application: Application):
    """
    Configure Telegram system Menu (commands list).
    """
    await application.bot.set_my_commands([
        BotCommand("book", "Записаться"),
        BotCommand("my", "Моя запись"),
        BotCommand("link", "Привязать аккаунт"),
        BotCommand("cancel", "Отменить ввод"),
        BotCommand("start", "Показать помощь"),
    ])
    # Make the chat "Menu" button open the command list
    await application.bot.set_chat_menu_button(menu_button=MenuButtonCommands())


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Я помогу записаться.\n\n"
        "Команды (также доступны в кнопке Menu):\n"
        "/book — записаться\n"
        "/my — посмотреть мою запись\n"
        "/link — привязать Telegram к пациенту (паспорт + дата рождения)\n"
        "/cancel — отменить ввод"
    )


async def my(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    identity = get_identity(user.id)
    if not identity or not identity.get("patient_id"):
        await update.message.reply_text(
            "Я пока не знаю, кто ты в базе.\n"
            "Сначала привяжи аккаунт: /link"
        )
        return

    row = get_my_appointment(user.id)
    if not row:
        await update.message.reply_text("У тебя пока нет записи. Напиши /book чтобы записаться.")
        return

    appt = row["appointment"]
    if isinstance(appt, str):
        appt_str = appt
    else:
        appt_local = appt.astimezone(TZ)
        appt_str = appt_local.strftime("%d.%m.%Y %H:%M")

    await update.message.reply_text(
        f"Твоя запись:\nФИО: {row['fio']}\nВремя: {appt_str}"
    )


async def link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Давай привяжем тебя к пациенту в базе.\n\n"
        "Отправь *паспорт* (как он хранится в базе: серия/номер).",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ASK_PASSPORT


async def ask_passport(update: Update, context: ContextTypes.DEFAULT_TYPE):
    passport = update.message.text.strip()
    if len(passport) < 5:
        await update.message.reply_text("Похоже, паспорт введён слишком коротко. Попробуй ещё раз.")
        return ASK_PASSPORT

    context.user_data["passport"] = passport
    await update.message.reply_text(
        "Теперь отправь *дату рождения*.\n"
        "Формат: `DD.MM.YYYY` (например 25.02.1999) или `YYYY-MM-DD`.",
        parse_mode="Markdown",
    )
    return ASK_BDATE


async def ask_bdate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bdate_iso = _parse_birth_date(update.message.text)
    if not bdate_iso:
        await update.message.reply_text("Не понял дату 😅 Введи как `25.02.1999` или `1999-02-25`.")
        return ASK_BDATE

    user = update.effective_user
    passport = context.user_data["passport"]

    patient = link_patient_by_passport_and_birthdate(
        tg_id=user.id,
        telegram_username=user.username,
        passport=passport,
        birth_date_iso=bdate_iso,
    )

    if not patient:
        await update.message.reply_text(
            "Не нашёл пациента с такими данными в базе.\n"
            "Проверь паспорт и дату рождения и попробуй снова: /link"
        )
        context.user_data.clear()
        return ConversationHandler.END

    context.user_data.clear()
    await update.message.reply_text(
        f"Готово ✅ Я привязал тебя к пациенту:\n{patient['fio']}\n\n"
        "Теперь можешь записываться: /book"
    )
    return ConversationHandler.END


async def book(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    identity = get_identity(user.id)
    if not identity or not identity.get("patient_id"):
        await update.message.reply_text(
            "Чтобы записаться, нужно привязать тебя к пациенту в базе.\n"
            "Запусти: /link"
        )
        return ConversationHandler.END

    await update.message.reply_text(
        "Отправь дату и время приёма.\n"
        "Формат: `YYYY-MM-DD HH:MM` или `DD.MM.YYYY HH:MM`\n"
        "Например: 2026-02-25 14:30\n\n"
        "Отмена: /cancel",
        parse_mode="Markdown",
    )
    return ASK_TIME


async def ask_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    dt = _parse_dt(text)
    if not dt:
        await update.message.reply_text(
            "Не понял дату/время 😅\n"
            "Напиши, например: 2026-02-25 14:30 или 25.02.2026 14:30\n\n"
            "Отмена: /cancel"
        )
        return ASK_TIME

    now = datetime.now(TZ)
    if dt < now:
        await update.message.reply_text(
            "Это время уже в прошлом. Введи, пожалуйста, будущее время.\n\n"
            "Отмена: /cancel"
        )
        return ASK_TIME

    user = update.effective_user
    identity = get_identity(user.id)
    if not identity or not identity.get("patient_id"):
        await update.message.reply_text("Потерял привязку. Запусти /link ещё раз.")
        return ConversationHandler.END

    patient_id = identity["patient_id"]

    # Если у тебя есть ФИО в patient/identity — подставь здесь.
    fio = f"PATIENT#{patient_id}"

    upsert_appointment_for_patient(patient_id, user.id, fio, dt.isoformat())

    await update.message.reply_text(
        f"Готово ✅\nЗаписал(а) на: {dt.strftime('%d.%m.%Y %H:%M')} (Yakutsk)\n\n"
        "Проверить запись: /my"
    )
    context.user_data.clear()
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Ок, отменил ввод.")
    return ConversationHandler.END


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set")

    init_db()

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    conv_link = ConversationHandler(
        entry_points=[CommandHandler("link", link)],
        states={
            ASK_PASSPORT: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_passport)],
            ASK_BDATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_bdate)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    conv_book = ConversationHandler(
        entry_points=[CommandHandler("book", book)],
        states={
            ASK_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_time)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("my", my))
    app.add_handler(conv_link)
    app.add_handler(conv_book)
    app.add_handler(CommandHandler("cancel", cancel))

    logger.info("Bot started (polling)...")
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
