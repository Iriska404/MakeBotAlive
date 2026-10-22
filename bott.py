"""Telegram-бот для игры «Киллер»."""
import html
import logging
import os

from telegram import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ChatType, ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from database import Database
from game import (
    KILL_POINTS,
    MIN_PLAYERS,
    NAME_MAX_LEN,
    NAME_MIN_LEN,
    SURVIVAL_POINTS,
    KillerGame,
)

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
log = logging.getLogger("killer-bot")

DB_PATH = os.getenv("DB_PATH", "killer.db")
# 1 — в группе объявлять, кто убил; 0 — писать только, кто выбыл (интереснее)
ANNOUNCE_KILLER = os.getenv("ANNOUNCE_KILLER", "0") == "1"

game = KillerGame(Database(DB_PATH))
HTML = ParseMode.HTML
esc = html.escape

# ----------------------------------------------------------------------- тексты
RULES = (
    "🎯 <b>Игра «Киллер»</b>\n\n"
    "<b>Как играть</b>\n"
    "1. В группе создатель отправляет /newgame. Каждый нажимает «Присоединиться», "
    "бот открывается в личке и просит вписать своё имя.\n"
    "2. Создатель нажимает «Начать игру» — каждому в личку приходит цель.\n"
    "3. Нужно «убить» цель в реальной жизни: коснуться её так, чтобы никто из живых "
    "игроков этого не видел.\n"
    "4. Отправьте боту /kill. Жертва подтверждает — и её цель переходит к вам.\n"
    "5. Игра заканчивается, когда остаётся два игрока.\n\n"
    "<b>Очки</b>\n"
    f"• за каждое убийство — {KILL_POINTS:g};\n"
    f"• за время жизни — до {SURVIVAL_POINTS:g} (доля игры, которую вы прожили: "
    "пережили всю игру — получите максимум).\n"
    "Побеждает набравший больше всех очков; при равенстве — у кого больше убийств."
)

HELP_PM = (
    "🎯 <b>Игра «Киллер»</b>\n\n"
    "/mytarget — узнать свою цель\n"
    "/kill — сообщить, что вы «убили» цель\n"
    "/rules — правила и подсчёт очков\n\n"
    "Чтобы вступить в игру, нажмите «Присоединиться» в сообщении игры в групповом чате."
)

HELP_GROUP = (
    "🎯 <b>Игра «Киллер»</b>\n\n"
    "/newgame — создать игру\n"
    "/invited — отправить сообщение с кнопкой вступления ещё раз\n"
    "/status — состояние игры\n"
    "/endgame — завершить игру или отменить набор (только создатель)\n"
    "/rules — правила"
)


# --------------------------------------------------------------------- помощники
def in_group(update: Update) -> bool:
    return update.effective_chat.type in (ChatType.GROUP, ChatType.SUPERGROUP)


def fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    parts = []
    if days:
        parts.append(f"{days} д")
    if days or hours:
        parts.append(f"{hours} ч")
    parts.append(f"{minutes} мин")
    return " ".join(parts)


async def safe_send(context, chat_id, text, **kwargs) -> bool:
    try:
        await context.bot.send_message(chat_id, text, parse_mode=HTML, **kwargs)
        return True
    except TelegramError as e:
        log.warning("Не удалось отправить сообщение %s: %s", chat_id, e)
        return False


async def send_long(context, chat_id, text):
    """Telegram ограничивает сообщение 4096 символами — режем по строкам."""
    chunk = ""
    for line in text.split("\n"):
        if len(chunk) + len(line) + 1 > 3800:
            await safe_send(context, chat_id, chunk)
            chunk = ""
        chunk += line + "\n"
    if chunk.strip():
        await safe_send(context, chat_id, chunk)


def lobby_view(context, g):
    players = game.list_players(g["id"])
    n = len(players)
    names = "\n".join(f"• {esc(p['name'])}" for p in players) or "—"
    text = (
        "🎮 <b>Игра «Киллер» — набор игроков</b>\n\n"
        f"<b>Участники ({n}):</b>\n{names}\n\n"
    )
    if n < MIN_PLAYERS:
        text += f"Для старта нужно минимум {MIN_PLAYERS} игрока (сейчас: {n})."
    else:
        text += "Можно начинать! Создатель игры нажимает «Начать игру»."
    text += "\n\nНажмите «Присоединиться», откройте бота и впишите своё имя."

    join_url = f"https://t.me/{context.bot.username}?start=join_{g['id']}"
    keyboard = [[InlineKeyboardButton("🎯 Присоединиться", url=join_url)]]
    if n >= MIN_PLAYERS:
        keyboard.append([InlineKeyboardButton("🚀 Начать игру", callback_data=f"start:{g['id']}")])
    return text, InlineKeyboardMarkup(keyboard)


async def send_lobby(context, game_id):
    g = game.get_game(game_id)
    text, markup = lobby_view(context, g)
    msg = await context.bot.send_message(g["chat_id"], text, reply_markup=markup, parse_mode=HTML)
    game.set_lobby_message(game_id, msg.message_id)


async def refresh_lobby(context, game_id):
    g = game.get_game(game_id)
    if not g or not g["lobby_message_id"]:
        return
    text, markup = lobby_view(context, g)
    try:
        await context.bot.edit_message_text(
            text,
            chat_id=g["chat_id"],
            message_id=g["lobby_message_id"],
            reply_markup=markup,
            parse_mode=HTML,
        )
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            log.warning("Не удалось обновить сообщение набора: %s", e)


def results_text(game_id, title) -> str:
    rows = game.scoreboard(game_id)
    g = game.get_game(game_id)
    lines = [title, ""]
    if rows:
        lines.append(f"🏆 <b>Победитель: {esc(rows[0].name)}</b> — {rows[0].score:.1f} очк.\n")
    lines.append("<b>Итоговая таблица:</b>")
    medals = ["🥇", "🥈", "🥉"]
    for i, r in enumerate(rows):
        place = medals[i] if i < 3 else f"{i + 1}."
        state = f"в игре {fmt_duration(r.alive_seconds)}"
        if r.alive:
            state += ", дожил(а) до конца"
        lines.append(f"{place} {esc(r.name)} — <b>{r.score:.1f}</b> (убийств: {r.kills}; {state})")
    if g and g["started_at"] and g["finished_at"]:
        lines.append(f"\n⏱ Длительность игры: {fmt_duration(g['finished_at'] - g['started_at'])}")
    lines.append(f"Очки: {KILL_POINTS:g} за убийство + до {SURVIVAL_POINTS:g} за время жизни.")
    return "\n".join(lines)


async def post_results(context, game_id, title="🏁 <b>Игра окончена!</b>"):
    g = game.get_game(game_id)
    await send_long(context, g["chat_id"], results_text(game_id, title))


# ------------------------------------------------------------------ общие команды
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type != ChatType.PRIVATE:
        await update.message.reply_text(HELP_GROUP, parse_mode=HTML)
        return

    if context.args and context.args[0].startswith("join_"):
        try:
            game_id = int(context.args[0][5:])
        except ValueError:
            await update.message.reply_text(HELP_PM, parse_mode=HTML)
            return
        await begin_join(update, game_id)
        return
    await update.message.reply_text(HELP_PM, parse_mode=HTML)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = HELP_GROUP if update.effective_chat.type != ChatType.PRIVATE else HELP_PM
    await update.message.reply_text(text, parse_mode=HTML)


async def rules_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(RULES, parse_mode=HTML)


# ------------------------------------------------------------------ вступление (ЛС)
async def begin_join(update: Update, game_id: int):
    code, name = game.begin_join(update.effective_user.id, game_id)
    if code == "closed":
        await update.message.reply_text("❌ Набор в эту игру уже закрыт или игра не найдена.")
    elif code == "already":
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("✏️ Сменить имя", callback_data=f"rename:{game_id}")]]
        )
        await update.message.reply_text(
            f"Вы уже в игре под именем <b>{esc(name)}</b>.", reply_markup=keyboard, parse_mode=HTML
        )
    else:
        await update.message.reply_text(
            "🎯 Как вас зовут?\n\n"
            "Пришлите имя, под которым вас знают участники игры "
            f"({NAME_MIN_LEN}–{NAME_MAX_LEN} символов). "
            "Именно его будут видеть остальные игроки."
        )


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Любой обычный текст в личке — это имя, если мы его ждём."""
    user = update.effective_user
    code, game_id, name = game.submit_name(user.id, user.username, update.message.text)

    if code == "no_pending":
        await update.message.reply_text(
            "Я сейчас не жду от вас сообщений. Чтобы вступить в игру, нажмите "
            "«Присоединиться» в сообщении игры в группе. Команды: /help"
        )
    elif code == "closed":
        await update.message.reply_text("❌ Набор в эту игру уже закрыт.")
    elif code == "bad_length":
        await update.message.reply_text(
            f"Имя должно быть от {NAME_MIN_LEN} до {NAME_MAX_LEN} символов. Попробуйте ещё раз."
        )
    elif code == "taken":
        await update.message.reply_text("Такое имя уже занято в этой игре. Придумайте другое.")
    else:  # joined / renamed
        verb = "Вы в игре" if code == "joined" else "Имя изменено"
        await update.message.reply_text(
            f"✅ {verb}: <b>{esc(name)}</b>.\n\n"
            "Дождитесь, пока создатель запустит игру — цель придёт сюда.",
            parse_mode=HTML,
        )
        await refresh_lobby(context, game_id)


# ----------------------------------------------------------------- команды в группе
async def newgame(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not in_group(update):
        await update.message.reply_text(
            "Игру нужно создавать в групповом чате: добавьте меня в группу и отправьте /newgame."
        )
        return
    chat, user = update.effective_chat, update.effective_user
    game_id = game.create_game(chat.id, chat.title, user.id)
    if game_id is None:
        await update.message.reply_text(
            "❌ В этом чате уже есть незавершённая игра. Посмотреть: /status"
        )
        return
    await send_lobby(context, game_id)


async def invited(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not in_group(update):
        return
    g = game.get_chat_game(update.effective_chat.id)
    if not g or g["status"] != "waiting":
        await update.message.reply_text("❌ Нет игры, к которой можно присоединиться.")
        return
    await send_lobby(context, g["id"])


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not in_group(update):
        return
    g = game.get_chat_game(update.effective_chat.id)
    if not g:
        await update.message.reply_text("❌ В этом чате нет активной игры. Создать: /newgame")
        return

    if g["status"] == "waiting":
        text, _ = lobby_view(context, g)
        await update.message.reply_text(text.split("\n\nНажмите")[0], parse_mode=HTML)
        return

    import time

    alive, dead = game.alive_and_dead(g["id"])
    alive_list = "\n".join(f"• {esc(p['name'])}" for p in alive) or "—"
    dead_list = "\n".join(f"• {esc(p['name'])}" for p in dead) or "—"
    await update.message.reply_text(
        f"🎯 <b>Игра идёт</b> ({fmt_duration(time.time() - g['started_at'])})\n\n"
        f"<b>В игре ({len(alive)}):</b>\n{alive_list}\n\n"
        f"<b>Выбыли ({len(dead)}):</b>\n{dead_list}",
        parse_mode=HTML,
    )


async def endgame(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not in_group(update):
        return
    g = game.get_chat_game(update.effective_chat.id)
    if not g:
        await update.message.reply_text("❌ В этом чате нет активной игры.")
        return
    if g["creator_id"] != update.effective_user.id:
        await update.message.reply_text("❌ Только создатель игры может её завершить.")
        return
    question = (
        "Отменить набор игроков?"
        if g["status"] == "waiting"
        else "Завершить игру досрочно? Победитель определится по текущим очкам."
    )
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Да", callback_data=f"end:yes:{g['id']}"),
                InlineKeyboardButton("↩️ Нет", callback_data=f"end:no:{g['id']}"),
            ]
        ]
    )
    await update.message.reply_text(question, reply_markup=keyboard)


# --------------------------------------------------------------------- команды в ЛС
async def pick_game_or_run(update, context, action, title, run):
    """Если игра одна — выполняем сразу, если несколько — даём выбрать."""
    games = game.user_active_games(update.effective_user.id)
    if not games:
        await update.message.reply_text(
            "Сейчас у вас нет активных игр, в которых вы ещё в игре."
        )
    elif len(games) == 1:
        await run(update.message.reply_text, games[0]["game_id"])
    else:
        keyboard = [
            [
                InlineKeyboardButton(
                    g["chat_title"] or f"Игра №{g['game_id']}", callback_data=f"{action}:{g['game_id']}"
                )
            ]
            for g in games
        ]
        await update.message.reply_text(
            f"Вы участвуете в нескольких играх. {title}",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )


async def mytarget(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    async def run(reply, game_id):
        await reply(target_text(game_id, user_id), parse_mode=HTML)

    await pick_game_or_run(update, context, "target", "Выберите игру:", run)


def target_text(game_id, user_id) -> str:
    name = game.get_target_name(game_id, user_id)
    if not name:
        return "❌ Цель не найдена."
    return f"🎯 <b>Ваша цель:</b> {esc(name)}"


async def kill(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    async def run(reply, game_id):
        await reply(await send_kill_request(context, user_id, game_id), parse_mode=HTML)

    await pick_game_or_run(update, context, "kill", "Выберите игру, где вы «убили» цель:", run)


async def send_kill_request(context, user_id, game_id) -> str:
    res = game.create_kill_request(game_id, user_id)
    errors = {
        "not_in_game": "❌ Вы не участвуете в этой игре или уже выбыли.",
        "not_active": "❌ Игра сейчас не идёт.",
        "no_target": "❌ У вас нет цели.",
    }
    if res["code"] != "ok":
        return errors[res["code"]]

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Да, меня «убили»", callback_data=f"kc:{res['request_id']}"),
                InlineKeyboardButton("❌ Нет", callback_data=f"kr:{res['request_id']}"),
            ]
        ]
    )
    sent = await safe_send(
        context,
        res["victim_user_id"],
        f"🔪 <b>{esc(res['killer_name'])}</b> заявляет, что «убил(а)» вас.\n\nЭто правда?",
        reply_markup=keyboard,
    )
    if not sent:
        game.expire_request(res["request_id"])
        return "❌ Не удалось отправить запрос жертве. Пусть она откроет бота и нажмёт /start."
    return f"✅ Запрос отправлен: <b>{esc(res['victim_name'])}</b>. Ждите подтверждения."


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Неизвестная команда. Используйте /help")


# ------------------------------------------------------------------------ кнопки
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    parts = (query.data or "").split(":")
    action = parts[0]
    try:
        if action == "start":
            await cb_start(query, context, int(parts[1]))
        elif action == "end":
            await cb_end(query, context, parts[1], int(parts[2]))
        elif action == "rename":
            await cb_rename(query, context, int(parts[1]))
        elif action == "target":
            await query.answer()
            await query.edit_message_text(
                target_text(int(parts[1]), query.from_user.id), parse_mode=HTML
            )
        elif action == "kill":
            await query.answer()
            text = await send_kill_request(context, query.from_user.id, int(parts[1]))
            await query.edit_message_text(text, parse_mode=HTML)
        elif action in ("kc", "kr"):
            await cb_victim_answer(query, context, int(parts[1]), confirm=(action == "kc"))
        else:
            await query.answer()
    except (ValueError, IndexError):
        await query.answer()
    except BadRequest as e:
        log.warning("BadRequest в callback: %s", e)


async def cb_start(query, context, game_id):
    res = game.start_game(game_id, query.from_user.id)
    code = res["code"]
    errors = {
        "not_found": "Игра не найдена.",
        "not_creator": "Только создатель игры может её начать.",
        "not_waiting": "Игра уже началась или завершена.",
    }
    if code in errors:
        await query.answer(errors[code], show_alert=True)
        return
    if code == "too_few":
        await query.answer(
            f"Нужно минимум {MIN_PLAYERS} игрока. Сейчас: {res['count']}", show_alert=True
        )
        return

    await query.answer("Игра началась!")
    failed = []
    for user_id, name, target in res["assignments"]:
        ok = await safe_send(
            context,
            user_id,
            f"🎯 <b>Игра началась!</b>\n\nВаша цель: <b>{esc(target)}</b>\n\n"
            "Коснитесь цели так, чтобы никто из живых игроков не видел, "
            "затем отправьте /kill. Напомнить цель: /mytarget",
        )
        if not ok:
            failed.append(name)

    text = (
        f"🚀 <b>Игра началась!</b> Игроков: {len(res['assignments'])}.\n"
        "Цели разосланы в личные сообщения. Статус: /status"
    )
    if failed:
        text += (
            "\n\n⚠️ Не смог написать: " + ", ".join(esc(n) for n in failed) +
            ". Пусть откроют бота и отправят /mytarget."
        )
    await query.edit_message_text(text, parse_mode=HTML)


async def cb_end(query, context, answer, game_id):
    if answer == "no":
        await query.answer()
        await query.edit_message_text("Хорошо, игра продолжается.")
        return
    code = game.finish_game(game_id, query.from_user.id)
    if code == "forbidden":
        await query.answer("Только создатель игры может это сделать.", show_alert=True)
        return
    await query.answer()
    if code == "cancelled":
        await query.edit_message_text("🚫 Набор отменён, игра не состоялась.")
    elif code == "finished":
        await query.edit_message_text("🏁 Игра завершена досрочно.")
        await post_results(context, game_id, "🏁 <b>Игра завершена досрочно!</b>")
    else:
        await query.edit_message_text("Эта игра уже завершена.")


async def cb_rename(query, context, game_id):
    if game.begin_rename(query.from_user.id, game_id):
        await query.answer()
        await query.edit_message_text("✏️ Пришлите новое имя одним сообщением.")
    else:
        await query.answer("Сменить имя уже нельзя: набор закрыт.", show_alert=True)


async def cb_victim_answer(query, context, request_id, confirm):
    r = game.resolve_kill(request_id, query.from_user.id, confirm)
    code = r["code"]

    if code == "forbidden":
        await query.answer("Эта кнопка не для вас.", show_alert=True)
        return
    await query.answer()

    if code in ("not_found", "stale"):
        await query.edit_message_text("Этот запрос уже обработан.")
        return
    if code == "invalid":
        await query.edit_message_text("Запрос устарел: ситуация в игре изменилась.")
        return

    if code == "rejected":
        await query.edit_message_text("❌ Вы отклонили запрос.")
        await safe_send(
            context,
            r["killer_user_id"],
            f"❌ <b>{esc(r['victim_name'])}</b> не подтвердил(а) убийство.\n"
            "Обсудите это лично. Если это была ошибка — отправьте /kill ещё раз.",
        )
        return

    # confirmed
    await query.edit_message_text(
        "☠️ Вы выбыли из игры. Спасибо, что играли! Итоги появятся в группе."
    )
    if r["finished"]:
        await safe_send(
            context,
            r["killer_user_id"],
            "🎯 <b>Убийство подтверждено!</b>\n\nИгра окончена — итоги в групповом чате.",
        )
    else:
        await safe_send(
            context,
            r["killer_user_id"],
            "🎯 <b>Убийство подтверждено!</b>\n\n"
            f"Ваша новая цель: <b>{esc(r['new_target_name'])}</b>\n"
            f"Осталось игроков: {r['alive_count']}",
        )

    announce = f"🔪 <b>{esc(r['victim_name'])}</b> выбыл(а) из игры!"
    if ANNOUNCE_KILLER:
        announce += f" Убийца: {esc(r['killer_name'])}."
    announce += f"\nОсталось игроков: {r['alive_count']}"
    await safe_send(context, r["chat_id"], announce)

    if r["finished"]:
        await post_results(context, r["game_id"])


# ------------------------------------------------------------------------ запуск
async def on_error(update, context: ContextTypes.DEFAULT_TYPE):
    log.error("Необработанная ошибка", exc_info=context.error)


async def post_init(app: Application):
    await app.bot.set_my_commands(
        [
            BotCommand("mytarget", "Моя цель"),
            BotCommand("kill", "Сообщить об убийстве"),
            BotCommand("rules", "Правила и очки"),
            BotCommand("help", "Помощь"),
        ],
        scope=BotCommandScopeAllPrivateChats(),
    )
    await app.bot.set_my_commands(
        [
            BotCommand("newgame", "Создать игру"),
            BotCommand("invited", "Позвать игроков ещё раз"),
            BotCommand("status", "Состояние игры"),
            BotCommand("endgame", "Завершить игру"),
            BotCommand("rules", "Правила"),
        ],
        scope=BotCommandScopeAllGroupChats(),
    )


def main():
    if not BOT_TOKEN:
        raise SystemExit("Не задан BOT_TOKEN. Скопируйте .env.example в .env и вставьте токен.")

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("rules", rules_command))

    # группа
    app.add_handler(CommandHandler("newgame", newgame))
    app.add_handler(CommandHandler("invited", invited))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("endgame", endgame))

    # личка
    private = filters.ChatType.PRIVATE
    app.add_handler(CommandHandler("mytarget", mytarget, filters=private))
    app.add_handler(CommandHandler("kill", kill, filters=private))
    app.add_handler(MessageHandler(private & filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(private & filters.COMMAND, unknown_command))

    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_error_handler(on_error)

    log.info("Бот запущен...")
    app.run_polling()


if __name__ == "__main__":
    main()
