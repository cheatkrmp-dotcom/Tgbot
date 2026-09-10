import os
import logging
from collections import defaultdict, deque

from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    TypeHandler,
)
import google.generativeai as genai

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("tg-business-bot")

# ---------- Конфиг ----------
BOT_TOKEN = os.environ["BOT_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")

PERSONA = os.environ.get(
    "PERSONA",
    "Ты отвечаешь в Telegram от лица владельца аккаунта. "
    "Пиши коротко, дружелюбно и по-человечески, как будто это пишет сам владелец, "
    "а не бот. Не упоминай, что ты ИИ или бот."
)

HISTORY_LEN = int(os.environ.get("HISTORY_LEN", "10"))
IGNORE_CHAT_IDS = {
    int(x) for x in os.environ.get("IGNORE_CHAT_IDS", "").split(",") if x.strip()
}

# ---------- Gemini ----------
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel(GEMINI_MODEL, system_instruction=PERSONA)

# ---------- Состояние (в памяти процесса) ----------
history: dict[int, deque] = defaultdict(lambda: deque(maxlen=HISTORY_LEN))
connection_owner: dict[str, int] = {}      # business_connection_id -> id владельца аккаунта
connection_enabled: dict[str, bool] = {}   # business_connection_id -> включена ли автоматизация
paused = False                             # ручная пауза командой /pause


def build_prompt(chat_id: int, text: str) -> str:
    convo = "\n".join(history[chat_id])
    if convo:
        return f"Предыдущая переписка:\n{convo}\n\nНовое сообщение собеседника: {text}\nТвой ответ:"
    return f"Сообщение собеседника: {text}\nТвой ответ:"


async def ask_gemini(chat_id: int, text: str) -> str:
    prompt = build_prompt(chat_id, text)
    response = await model.generate_content_async(prompt)
    return (response.text or "").strip()


async def on_business_connection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Срабатывает при подключении/отключении/изменении бота в Telegram Business."""
    conn = update.business_connection
    if conn is None:
        return
    connection_owner[conn.id] = conn.user.id
    connection_enabled[conn.id] = conn.is_enabled
    log.info("Business connection %s: user=%s enabled=%s", conn.id, conn.user.id, conn.is_enabled)


async def on_business_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Срабатывает на новое сообщение в чате, подключённом через Telegram Business."""
    message = update.business_message
    if message is None or message.chat.type != "private":
        return

    bc_id = message.business_connection_id
    if paused or not connection_enabled.get(bc_id, True):
        return
    if message.chat_id in IGNORE_CHAT_IDS:
        return

    owner_id = connection_owner.get(bc_id)
    if owner_id is None:
        try:
            conn = await context.bot.get_business_connection(bc_id)
            owner_id = conn.user.id
            connection_owner[bc_id] = owner_id
            connection_enabled[bc_id] = conn.is_enabled
        except Exception:
            log.exception("Не удалось получить business connection %s", bc_id)

    if message.from_user and owner_id is not None and message.from_user.id == owner_id:
        return  # это ты сам написал вручную с телефона — не вмешиваемся
    if message.from_user and message.from_user.is_bot:
        return

    text = message.text or message.caption or ""
    if not text.strip():
        return

    try:
        reply = await ask_gemini(message.chat_id, text)
        if not reply:
            return
        await context.bot.send_message(
            chat_id=message.chat_id,
            text=reply,
            business_connection_id=bc_id,
        )
        history[message.chat_id].append(f"Собеседник: {text}")
        history[message.chat_id].append(f"Ты: {reply}")
    except Exception:
        log.exception("Ошибка при обработке business-сообщения из чата %s", message.chat_id)


async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global paused
    paused = True
    await update.message.reply_text("Автоответы приостановлены.")


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global paused
    paused = False
    await update.message.reply_text("Автоответы снова включены.")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = "на паузе" if paused else "активен"
    connections = ", ".join(connection_owner.keys()) or "пока нет подключений"
    await update.message.reply_text(f"Статус: {state}\nПодключения: {connections}")


def main() -> None:
    app: Application = ApplicationBuilder().token(BOT_TOKEN).build()

    # Управляющие команды — пиши их боту напрямую в его личный чат (не через Business)
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("status", cmd_status))

    # События автоматизации Telegram Business (не обычные Message-апдейты)
    app.add_handler(TypeHandler(Update, on_business_connection), group=0)
    app.add_handler(TypeHandler(Update, on_business_message), group=1)

    log.info("Бот запущен, ждёт подключения через настройки Telegram Business...")
    app.run_polling(
        allowed_updates=[
            "message",
            "business_connection",
            "business_message",
            "edited_business_message",
        ]
    )


if __name__ == "__main__":
    main()
