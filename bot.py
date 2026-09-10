import os
import time
import asyncio
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

BOT_VERSION = "0.2.2"
START_TIME = time.time()

# ---------- Конфиг ----------
BOT_TOKEN = os.environ["BOT_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-lite")

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
messages_answered = 0                      # сколько раз бот успешно ответил
messages_failed = 0                        # сколько раз бот словил ошибку (например лимит Gemini)
active_chats: set[int] = set()             # id чатов, где бот уже отвечал


def build_prompt(chat_id: int, text: str) -> str:
    convo = "\n".join(history[chat_id])
    if convo:
        return f"Предыдущая переписка:\n{convo}\n\nНовое сообщение собеседника: {text}\nТвой ответ:"
    return f"Сообщение собеседника: {text}\nТвой ответ:"


async def ask_gemini(chat_id: int, text: str, retries: int = 2) -> str:
    prompt = build_prompt(chat_id, text)
    for attempt in range(retries + 1):
        try:
            response = await model.generate_content_async(prompt)
            return (response.text or "").strip()
        except Exception as e:
            is_rate_limit = "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e)
            if is_rate_limit and attempt < retries:
                wait = 15 * (attempt + 1)  # 15s, потом 30s
                log.warning("Лимит Gemini, жду %sс и пробую снова (попытка %s)", wait, attempt + 1)
                await asyncio.sleep(wait)
                continue
            raise


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
    global messages_answered, messages_failed
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
        messages_answered += 1
        active_chats.add(message.chat_id)
    except Exception as e:
        messages_failed += 1
        if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
            log.warning("Достигнут лимит Gemini API (чат %s): %s", message.chat_id, e)
        else:
            log.exception("Ошибка при обработке business-сообщения из чата %s", message.chat_id)


START_TEXT = (
    f"👋 Привет! Я твой авто-ответчик для Telegram (версия {BOT_VERSION}).\n\n"
    "Я подключаюсь к твоему аккаунту через Telegram Business и отвечаю за тебя "
    "на входящие личные сообщения, используя Gemini.\n\n"
    "Как настроить:\n"
    "1. Settings → Telegram для бизнеса → Чат-боты → добавь меня по username\n"
    "2. Выбери, к каким чатам у меня будет доступ\n"
    "3. Готово — я начну отвечать автоматически\n\n"
    "Команды (пиши их мне лично, не через Business):\n"
    "/status — статус и активные подключения\n"
    "/stats — статистика ответов\n"
    "/pause — приостановить автоответы\n"
    "/resume — снова включить автоответы\n"
    "/help — показать это сообщение ещё раз"
)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(START_TEXT)


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uptime_seconds = int(time.time() - START_TIME)
    hours, rem = divmod(uptime_seconds, 3600)
    minutes = rem // 60
    total = messages_answered + messages_failed
    success_rate = f"{(messages_answered / total * 100):.0f}%" if total else "—"
    await update.message.reply_text(
        f"📊 Статистика (версия {BOT_VERSION})\n"
        f"Аптайм: {hours}ч {minutes}м\n"
        f"Отвечено сообщений: {messages_answered}\n"
        f"Ошибок (в т.ч. лимиты Gemini): {messages_failed}\n"
        f"Успешных ответов: {success_rate}\n"
        f"Активных чатов: {len(active_chats)}\n"
        f"Модель: {GEMINI_MODEL}"
    )


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
    await update.message.reply_text(
        f"Версия: {BOT_VERSION}\nСтатус: {state}\nПодключения: {connections}"
    )


def main() -> None:
    app: Application = ApplicationBuilder().token(BOT_TOKEN).build()

    # Управляющие команды — пиши их боту напрямую в его личный чат (не через Business)
    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("stats", cmd_stats))

    # События автоматизации Telegram Business (не обычные Message-апдейты)
    app.add_handler(TypeHandler(Update, on_business_connection), group=0)
    app.add_handler(TypeHandler(Update, on_business_message), group=1)

    log.info("Бот v%s запущен, ждёт подключения через настройки Telegram Business...", BOT_VERSION)
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
