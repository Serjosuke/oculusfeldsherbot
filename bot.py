import os
import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram import Update, ReplyKeyboardMarkup
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

# ===== ReplyKeyboard "big buttons" =====
BTN_BOOK = "📅 Записаться"
BTN_MY = "📄 Моя запись"
BTN_LINK = "🔗 Привязать аккаунт"
BTN_CANCEL = "❌ Отмена"

MAIN_KB = ReplyKeyboardMarkup(
    [
        [BTN_BOOK, BTN_MY],
        [BTN_LINK, BTN_CANCEL],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

BOOK_KB = ReplyKeyboardMarkup(
    [
        ["2026-02-25 14:30", "2026-02-25 15:00"],
        [BTN_BOOK, BTN_MY],
        [BTN_LINK, BTN_CANCEL],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

BUTTON_TO_CMD = {
    BTN_BOOK: "book",
    BTN_MY: "my",
    BTN_LINK: "link",
    BTN_CANCEL: "cancel",
}


def _parse_dt(text: str) -> datetime | None:
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
    text = text.strip()
    for f in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            d = datetime.strptime(text, f).date()
            return d.isoformat()
        except ValueError:
            pass
    return None


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Выберите действие кнопками ниже 👇",
        reply_markup=MAIN_KB,
    )


async def my(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    identity = get_identity(user.id)
    if not identity or not identity.get("patient_id"):
        await update.message.reply_text(
            "Я пока не знаю, кто ты в базе.\n"
            "Сначала привяжи аккаунт: нажми «🔗 Привязать аккаунт».",
            reply_markup=MAIN_KB,
        )
        return

    row = get_my_appointment(user.id)
    if not row:
        await update.message.reply_text(
            "У тебя пока нет записи. Нажми «📅 Записаться».",
            reply_markup=MAIN_KB,
        )
        return

    appt = row["appointment"]
    if isinstance(appt, str):
        appt_str = appt
    else:
        appt_local = appt.astimezone(TZ)
        appt_str = appt_local.strftime("%d.%m.%Y %H:%M")

    await update.message.reply_text(
        f"Твоя запись:\nФИО: {row['fio']}\nВремя: {appt_str}",
        reply_markup=MAIN_KB,
    )


async def link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Отправь паспорт в формате:\n"
        "1234 567890\n\n"
        "(4 цифры серия, пробел, 6 цифр номер)",
        reply_markup=MAIN_KB,
    )
    return ASK_PASSPORT


async def ask_passport(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    # allow menu buttons during conversation
    if text in BUTTON_TO_CMD:
        return await _route_button(update, context, text)

    # Формат: 4 цифры + пробел + 6 цифр
    if not re.fullmatch(r"\d{4} \d{6}", text):
        await update.message.reply_text(
            "Неверный формат паспорта.\n\n"
            "Введите так:\n"
            "1234 567890\n\n"
            "(4 цифры серия, пробел, 6 цифр номер)",
            reply_markup=MAIN_KB,
        )
        return ASK_PASSPORT

    context.user_data["passport"] = text

    context.user_data["passport"] = text
    await update.message.reply_text(
        "Теперь отправь *дату рождения*.\n"
        "Формат: `DD.MM.YYYY` (например 25.02.1999) или `YYYY-MM-DD`.\n\n"
        "Отмена: «❌ Отмена».",
        parse_mode="Markdown",
        reply_markup=MAIN_KB,
    )
    return ASK_BDATE


async def ask_bdate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    # allow menu buttons during conversation
    if text in BUTTON_TO_CMD:
        return await _route_button(update, context, text)

    bdate_iso = _parse_birth_date(text)
    if not bdate_iso:
        await update.message.reply_text(
            "Не понял дату 😅 Введи как `25.02.1999` или `1999-02-25`.\n\n"
            "Отмена: «❌ Отмена».",
            parse_mode="Markdown",
            reply_markup=MAIN_KB,
        )
        return ASK_BDATE

    user = update.effective_user
    passport = context.user_data.get("passport", "")

    patient = link_patient_by_passport_and_birthdate(
        tg_id=user.id,
        telegram_username=user.username,
        passport=passport,
        birth_date_iso=bdate_iso,
    )

    if not patient:
        context.user_data.clear()
        await update.message.reply_text(
            "Не нашёл пациента с такими данными в базе.\n"
            "Проверь паспорт и дату рождения и попробуй снова: «🔗 Привязать аккаунт».",
            reply_markup=MAIN_KB,
        )
        return ConversationHandler.END

    context.user_data.clear()
    await update.message.reply_text(
        f"Готово ✅ Я привязал тебя к пациенту:\n{patient['fio']}\n\n"
        "Теперь можешь записываться: «📅 Записаться».",
        reply_markup=MAIN_KB,
    )
    return ConversationHandler.END


async def book(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    identity = get_identity(user.id)
    if not identity or not identity.get("patient_id"):
        await update.message.reply_text(
            "Чтобы записаться, нужно привязать тебя к пациенту в базе.\n"
            "Нажми «🔗 Привязать аккаунт».",
            reply_markup=MAIN_KB,
        )
        return ConversationHandler.END

    await update.message.reply_text(
        "Отправь дату и время приёма.\n"
        "Формат: `YYYY-MM-DD HH:MM` или `DD.MM.YYYY HH:MM`\n"
        "Например: 2026-02-25 14:30\n\n"
        "Отмена: «❌ Отмена».",
        parse_mode="Markdown",
        reply_markup=BOOK_KB,
    )
    return ASK_TIME


async def ask_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    # allow menu buttons during conversation
    if text in BUTTON_TO_CMD:
        return await _route_button(update, context, text)

    dt = _parse_dt(text)
    if not dt:
        await update.message.reply_text(
            "Не понял дату/время 😅\n"
            "Напиши, например: 2026-02-25 14:30 или 25.02.2026 14:30\n\n"
            "Отмена: «❌ Отмена».",
            reply_markup=BOOK_KB,
        )
        return ASK_TIME

    now = datetime.now(TZ)
    if dt < now:
        await update.message.reply_text(
            "Это время уже в прошлом. Введи, пожалуйста, будущее время.\n\n"
            "Отмена: «❌ Отмена».",
            reply_markup=BOOK_KB,
        )
        return ASK_TIME

    user = update.effective_user
    identity = get_identity(user.id)
    if not identity or not identity.get("patient_id"):
        await update.message.reply_text(
            "Потерял привязку. Нажми «🔗 Привязать аккаунт» ещё раз.",
            reply_markup=MAIN_KB,
        )
        return ConversationHandler.END

    patient_id = identity["patient_id"]

    # Если захочешь — можно подтянуть fio из patients отдельной функцией.
    fio = f"PATIENT#{patient_id}"

    upsert_appointment_for_patient(patient_id, user.id, fio, dt.isoformat())

    await update.message.reply_text(
        f"Готово ✅\nЗаписал(а) на: {dt.strftime('%d.%m.%Y %H:%M')} (Yakutsk)\n\n"
        "Проверить: «📄 Моя запись».",
        reply_markup=MAIN_KB,
    )
    context.user_data.clear()
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Ок, отменил ввод.", reply_markup=MAIN_KB)
    return ConversationHandler.END


async def _route_button(update: Update, context: ContextTypes.DEFAULT_TYPE, button_text: str):
    """
    Route ReplyKeyboard button to the same logic as commands.
    Also ends any active conversation state when appropriate.
    """
    cmd = BUTTON_TO_CMD.get(button_text)

    if cmd == "book":
        return await book(update, context)
    if cmd == "my":
        await my(update, context)
        return ConversationHandler.END
    if cmd == "link":
        return await link(update, context)
    if cmd == "cancel":
        return await cancel(update, context)

    return ConversationHandler.END


async def menu_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Global handler for main menu buttons when user is not inside a ConversationHandler state.
    """
    text = update.message.text.strip()
    if text in BUTTON_TO_CMD:
        return await _route_button(update, context, text)


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set")

    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    conv_link = ConversationHandler(
        entry_points=[
            CommandHandler("link", link),
        ],
        states={
            ASK_PASSPORT: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_passport)],
            ASK_BDATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_bdate)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    conv_book = ConversationHandler(
        entry_points=[
            CommandHandler("book", book),
        ],
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

    # IMPORTANT: this goes AFTER conversation handlers
    btn_pattern = f"^({re.escape(BTN_BOOK)}|{re.escape(BTN_MY)}|{re.escape(BTN_LINK)}|{re.escape(BTN_CANCEL)})$"
    app.add_handler(MessageHandler(filters.Regex(btn_pattern), menu_buttons))

    logger.info("Bot started (polling)...")
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
