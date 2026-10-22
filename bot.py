"""Telegram-бот: ссылка на пост Instagram + текст -> медиа в канал, реакция в телеге, лайк в инсте.

Instagram:
  * вход и скачивание — всегда Instaloader;
  * лайк (IG_LIKE=true) — instagrapi, подключается только если лайк включён;
  * пароль нигде не хранится: если сессии нет или она невалидна, он запрашивается
    в терминале (getpass) и используется только для логина.
Сессия сохраняется стандартным файлом Instaloader (~/.config/instaloader/session-<логин>).
"""
import asyncio
import getpass
import logging
import os
import re
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

import instaloader
from dotenv import load_dotenv
from instaloader.instaloader import get_default_session_filename
from telegram import InputMediaPhoto, InputMediaVideo, LinkPreviewOptions, ReactionTypeEmoji, Update
from telegram.error import BadRequest
from telegram.ext import Application, ContextTypes, MessageHandler, filters

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
# httpx на INFO пишет каждый запрос к Telegram (getUpdates и т.д., вместе с токеном в URL) — глушим
for _noisy in ("httpx", "httpcore", "hpack"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)
log = logging.getLogger("ig_repost_bot")


def env_flag(name: str, default: str = "true") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


BOT_TOKEN = os.environ["BOT_TOKEN"]
CHANNEL_ID = os.environ["CHANNEL_ID"]  # @channelname или -100xxxxxxxxxx
ALLOWED_USERS = {int(x) for x in os.environ["ALLOWED_USER_IDS"].split(",") if x.strip()}
IG_LIKE = env_flag("IG_LIKE")  # лайк в Instagram
REACTION_EMOJI = os.getenv("REACTION_EMOJI", "😁")  # реакция бота на пост в канале
PENDING_TIMEOUT = float(os.getenv("PENDING_TIMEOUT", "60"))  # сколько ждать текст после ссылки, с
DOWNLOAD_TIMEOUT = float(os.getenv("DOWNLOAD_TIMEOUT", "180"))  # потолок на скачивание поста, с

IG_URL_RE = re.compile(
    r"https?://(?:www\.)?instagram\.com/(?:[\w.]+/)?(?:p|reel|reels|tv)/[\w-]+/?(?:\?\S*)?",
    re.IGNORECASE,
)
SHORTCODE_RE = re.compile(r"/(?:p|reel|reels|tv)/([\w-]+)")

# Лимиты Bot API
CAPTION_LIMIT = 1024
BOT_FILE_LIMIT = 50 * 1024 * 1024

SESSION_DIR = Path(get_default_session_filename("x")).parent
IGRAPI_SETTINGS = Path("ig_settings.json")  # устройство/сессия instagrapi (только при IG_LIKE=true)

IL_AUTH_ERRORS = (instaloader.LoginRequiredException, instaloader.LoginException)


# ---------------------------------------------------------------------------
# Instagram
# ---------------------------------------------------------------------------


class LoggingRateController(instaloader.RateController):
    """Стандартный контроллер лимитов, но с логом, когда Instaloader засыпает."""

    def sleep(self, secs: float):
        if secs > 1:
            log.warning("Instagram: лимит запросов, Instaloader ждёт %.0f с", secs)
        super().sleep(secs)


def _new_instaloader() -> instaloader.Instaloader:
    # max_connection_attempts оставляем стандартным (3 попытки, как в CLI Instaloader);
    # request_timeout короче стандартных 300 с, чтобы зависшее соединение не держало бота минутами
    return instaloader.Instaloader(
        quiet=True,
        save_metadata=False,
        download_comments=False,
        request_timeout=30,
        rate_controller=lambda ctx: LoggingRateController(ctx),
    )


def _saved_usernames() -> list[str]:
    return sorted(p.name[len("session-"):] for p in SESSION_DIR.glob("session-*"))


def _fetch(L: instaloader.Instaloader, url: str, path: Path) -> Path:
    # Важно: через get_and_write_raw — он качает с CDN анонимной сессией.
    # Сессия с логином шлёт Host/Origin от www.instagram.com, и CDN отвечает 404.
    t0 = time.monotonic()
    L.context.get_and_write_raw(url, str(path))
    log.info("  файл %s скачан: %.1f МБ за %.1f с", path.name, path.stat().st_size / 1048576, time.monotonic() - t0)
    return path


def _download(L: instaloader.Instaloader, url: str, dest: Path):
    """Скачивает медиа. Возвращает (media_id, [(тип, путь), ...])."""
    m = SHORTCODE_RE.search(url)
    if not m:
        raise ValueError("Не удалось определить shortcode поста")
    code = m.group(1)

    t0 = time.monotonic()
    log.info("[%s] Instagram: запрашиваю метаданные поста", code)
    post = instaloader.Post.from_shortcode(L.context, code)
    log.info("[%s] Instagram: метаданные получены за %.1f с, тип: %s", code, time.monotonic() - t0, post.typename)

    files = []
    if post.typename == "GraphSidecar":  # карусель
        nodes = list(post.get_sidecar_nodes())
        log.info("[%s] Instagram: карусель, элементов: %d", code, len(nodes))
        for i, node in enumerate(nodes):
            if node.is_video:
                files.append(("video", _fetch(L, node.video_url, dest / f"{code}_{i}.mp4")))
            else:
                files.append(("photo", _fetch(L, node.display_url, dest / f"{code}_{i}.jpg")))
    elif post.is_video:  # видео / reel
        log.info("[%s] Instagram: скачиваю видео", code)
        files.append(("video", _fetch(L, post.video_url, dest / f"{code}.mp4")))
    else:  # фото
        log.info("[%s] Instagram: скачиваю фото", code)
        files.append(("photo", _fetch(L, post.url, dest / f"{code}.jpg")))

    return f"{post.mediaid}_{post.owner_id}", files


class Instagram:
    def __init__(self, like: bool):
        self.like_enabled = like
        self.username: str | None = None
        self.il: instaloader.Instaloader | None = None
        self.ig = None  # instagrapi.Client, только если like_enabled
        self._lock = threading.RLock()

    # --- вход -------------------------------------------------------------

    def _login_instaloader(self, fresh: bool) -> instaloader.Instaloader:
        """Сохранённая сессия -> иначе интерактивный вход (пароль не сохраняется).
        fresh=True — сохранённая сессия известна как невалидная, сразу идём на вход."""
        username = os.getenv("IG_USERNAME", "").strip() or self.username or ""
        if not username and not fresh:
            saved = _saved_usernames()
            if len(saved) == 1:
                username = saved[0]

        if username and not fresh:
            try:
                L = _new_instaloader()
                L.load_session_from_file(username)
                if L.test_login():
                    log.info("Instaloader: сессия %s валидна", username)
                    self.username = username
                    return L
                log.info("Instaloader: сессия %s невалидна", username)
            except FileNotFoundError:
                log.info("Instaloader: сохранённой сессии для %s нет", username)
            except Exception as e:
                log.warning("Instaloader: не удалось загрузить сессию: %s", e)

        if not sys.stdin.isatty():
            raise RuntimeError(
                "Сессия Instagram отсутствует или истекла, а ввод пароля недоступен — "
                "запусти бота в терминале и введи пароль"
            )

        if not username:
            username = input("Instagram логин: ").strip()

        for attempt in range(3):
            password = getpass.getpass(f"Instagram пароль для {username}: ")
            L = _new_instaloader()
            try:
                L.login(username, password)
            except instaloader.BadCredentialsException:
                print("Неверный пароль, попробуй ещё раз." if attempt < 2 else "Неверный пароль.")
                continue
            except instaloader.TwoFactorAuthRequiredException:
                L.two_factor_login(input("Код 2FA: ").strip())
            finally:
                password = None  # пароль нигде не сохраняется
            L.save_session_to_file()
            self.username = username
            return L
        raise RuntimeError("Не удалось войти: неверный пароль")

    def _login_instagrapi(self, fresh: bool):
        from instagrapi import Client

        cl = Client()
        cl.delay_range = [1, 3]
        if not fresh and IGRAPI_SETTINGS.exists():
            cl.load_settings(IGRAPI_SETTINGS)
        cl.login_by_sessionid(self.il.context._session.cookies["sessionid"])
        cl.dump_settings(IGRAPI_SETTINGS)
        return cl

    def start(self, fresh: bool = False):
        with self._lock:
            self.il = self._login_instaloader(fresh)
            self.ig = self._login_instagrapi(fresh) if self.like_enabled else None

    def _relogin(self):
        """Перечитывает .env (IG_USERNAME) и входит заново с нуля."""
        load_dotenv(override=True)
        self.start(fresh=True)

    # --- операции ---------------------------------------------------------

    def download(self, url: str, dest: Path):
        with self._lock:
            try:
                return _download(self.il, url, dest)
            except IL_AUTH_ERRORS as e:
                log.warning("Ошибка авторизации Instaloader (%s) — вхожу заново", e)
                self._relogin()
                return _download(self.il, url, dest)

    def like(self, media_id: str) -> bool:
        from instagrapi.exceptions import (
            BadPassword,
            ChallengeRequired,
            ClientLoginRequired,
            ClientUnauthorizedError,
            LoginRequired,
            ReloginAttemptExceeded,
        )

        auth_errors = (
            LoginRequired,
            ClientLoginRequired,
            ClientUnauthorizedError,
            BadPassword,
            ChallengeRequired,
            ReloginAttemptExceeded,
        )
        with self._lock:
            try:
                return self.ig.media_like(media_id)
            except auth_errors as e:
                log.warning("Ошибка авторизации instagrapi (%s) — вхожу заново", e)
                self._relogin()
                return self.ig.media_like(media_id)


instagram = Instagram(like=IG_LIKE)


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------


async def post_to_channel(context: ContextTypes.DEFAULT_TYPE, files, text: str):
    bot = context.bot
    for _, path in files:
        if Path(path).stat().st_size > BOT_FILE_LIMIT:
            raise ValueError(f"{Path(path).name} больше 50 МБ — лимит Bot API")

    # Если текст не влезает в подпись — отправляем отдельным сообщением
    long_text = len(text) > CAPTION_LIMIT
    caption = None if long_text else (text or None)

    first_msg = None
    if len(files) == 1:
        kind, path = files[0]
        with open(path, "rb") as f:
            if kind == "photo":
                first_msg = await bot.send_photo(CHANNEL_ID, f, caption=caption)
            else:
                first_msg = await bot.send_video(CHANNEL_ID, f, caption=caption, supports_streaming=True)
    else:
        # В одном альбоме максимум 10 элементов
        for i in range(0, len(files), 10):
            chunk = files[i : i + 10]
            handles = [open(p, "rb") for _, p in chunk]
            try:
                media = []
                for j, ((kind, _), h) in enumerate(zip(chunk, handles)):
                    cap = caption if (i == 0 and j == 0) else None
                    if kind == "photo":
                        media.append(InputMediaPhoto(h, caption=cap))
                    else:
                        media.append(InputMediaVideo(h, caption=cap, supports_streaming=True))
                sent = await bot.send_media_group(CHANNEL_ID, media)
                if first_msg is None:
                    first_msg = sent[0]
            finally:
                for h in handles:
                    h.close()

    if long_text:
        await bot.send_message(CHANNEL_ID, text)

    return first_msg


async def safe_edit(status, text: str, **kwargs):
    """edit_text, который не падает, если текст не изменился (Telegram: 'Message is not modified')."""
    try:
        await status.edit_text(text, **kwargs)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise


# Пост, ссылка на который пришла без текста: ждём текст следующим сообщением
pending: dict[int, dict] = {}
publish_lock = asyncio.Lock()  # один пост за раз


async def process_post(context: ContextTypes.DEFAULT_TYPE, status, url: str, text: str):
    """Скачать -> опубликовать в канал -> реакция -> лайк. status — сообщение, которое обновляем."""
    code = SHORTCODE_RE.search(url).group(1)
    t0 = time.monotonic()
    async with publish_lock:
        log.info("[%s] ▶ Старт. Текст: %d симв.", code, len(text))
        tmp = Path(tempfile.mkdtemp(prefix="igbot_"))
        try:
            await safe_edit(status, "⏳ Скачиваю…")
            try:
                media_id, files = await asyncio.wait_for(
                    asyncio.to_thread(instagram.download, url, tmp), timeout=DOWNLOAD_TIMEOUT
                )
            except asyncio.TimeoutError:
                raise RuntimeError(f"Instagram не ответил за {DOWNLOAD_TIMEOUT:.0f} с") from None
            log.info("[%s] Скачано файлов: %d (%.1f с)", code, len(files), time.monotonic() - t0)

            await safe_edit(status, "📤 Публикую в канал…")
            log.info("[%s] Telegram: отправляю в канал %s", code, CHANNEL_ID)
            first_msg = await post_to_channel(context, files, text)
            log.info("[%s] Telegram: опубликовано, message_id=%s", code, first_msg.message_id)

            try:
                await context.bot.set_message_reaction(
                    CHANNEL_ID, first_msg.message_id, reaction=ReactionTypeEmoji(REACTION_EMOJI)
                )
                log.info("[%s] Telegram: реакция %s поставлена", code, REACTION_EMOJI)
                notes = [f"{REACTION_EMOJI} реакция в телеге"]
            except Exception as e:
                log.exception("[%s] Telegram: реакция не поставилась", code)
                notes = [f"⚠️ реакция в телеге не поставилась: {e}"]

            if IG_LIKE:
                try:
                    log.info("[%s] Instagram: ставлю лайк", code)
                    liked = await asyncio.wait_for(
                        asyncio.to_thread(instagram.like, media_id), timeout=DOWNLOAD_TIMEOUT
                    )
                    log.info("[%s] Instagram: лайк %s", code, "поставлен" if liked else "не поставился")
                    notes.append("❤️ лайк в инсте поставлен" if liked else "⚠️ лайк в инсте не поставился")
                except Exception as e:
                    log.exception("[%s] Instagram: ошибка лайка", code)
                    notes.append(f"⚠️ лайк в инсте не поставился: {e or type(e).__name__}")

            count = f" ({len(files)} шт.)" if len(files) > 1 else ""
            await safe_edit(status, f"✅ Опубликовано{count}\n" + "\n".join(notes))
            log.info("[%s] ✅ Готово за %.1f с", code, time.monotonic() - t0)
        except Exception as e:
            log.exception("[%s] ❌ Ошибка после %.1f с", code, time.monotonic() - t0)
            err = str(e) or type(e).__name__
            if len(err) > 300:
                err = err[:300] + "…"
            try:
                await safe_edit(
                    status, f"❌ Ошибка: {err}", link_preview_options=LinkPreviewOptions(is_disabled=True)
                )
            except Exception:
                log.exception("[%s] не удалось показать ошибку в чате", code)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


async def flush_pending(user_id: int, context: ContextTypes.DEFAULT_TYPE, reason: str):
    """Публикует ожидающий пост без текста (таймаут или пришла новая ссылка)."""
    p = pending.pop(user_id, None)
    if not p:
        return
    if p["task"] is not asyncio.current_task():
        p["task"].cancel()
    log.info("Ожидающий пост %s уходит без текста (%s)", p["url"], reason)
    await process_post(context, p["status"], p["url"], "")


async def _pending_timeout(user_id: int, context: ContextTypes.DEFAULT_TYPE):
    try:
        await asyncio.sleep(PENDING_TIMEOUT)
    except asyncio.CancelledError:
        return
    await flush_pending(user_id, context, f"текст не пришёл за {PENDING_TIMEOUT:.0f} с")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    if not msg or not msg.text or user.id not in ALLOWED_USERS:
        return

    m = IG_URL_RE.search(msg.text)

    if m:
        url = m.group(0)
        text = (msg.text[: m.start()] + msg.text[m.end() :]).strip()
        code = SHORTCODE_RE.search(url).group(1)
        log.info("📩 Сообщение от %s: ссылка %s, текст в сообщении: %d симв.", user.id, code, len(text))

        # Предыдущая ссылка без текста уходит как есть
        await flush_pending(user.id, context, "пришла новая ссылка")

        if text:
            status = await msg.reply_text("⏳ Скачиваю…")
            await process_post(context, status, url, text)
        else:
            status = await msg.reply_text(f"🔗 Ссылка принята, жду текст ({PENDING_TIMEOUT:.0f} с)…")
            task = context.application.create_task(_pending_timeout(user.id, context))
            pending[user.id] = {"url": url, "status": status, "task": task}
            log.info("[%s] Жду текст следующим сообщением (до %.0f с)", code, PENDING_TIMEOUT)
        return

    # Сообщение без ссылки
    p = pending.pop(user.id, None)
    if p:
        p["task"].cancel()
        text = msg.text.strip()
        log.info("📩 Сообщение от %s: текст %d симв. — к ожидающей ссылке %s", user.id, len(text), p["url"])
        await process_post(context, p["status"], p["url"], text)
    else:
        log.info("📩 Сообщение от %s без ссылки и без ожидающего поста — игнорирую", user.id)
        await msg.reply_text("Пришли ссылку на пост Instagram и текст (в том же сообщении или следующим).")


async def wake_watcher(interval: float = 2.0, threshold: float = 10.0):
    """Только пишет в лог, что Mac спал (в т. ч. короткий DarkWake). Сон не блокирует и не будит:
    time.monotonic() во сне не идёт, time.time() идёт — по разнице видно, сколько Mac проспал."""
    last_wall, last_mono = time.time(), time.monotonic()
    while True:
        await asyncio.sleep(interval)
        wall, mono = time.time(), time.monotonic()
        slept = (wall - last_wall) - (mono - last_mono)
        if slept > threshold:
            log.info("💤 Mac спал ~%.0f с, процесс снова работает (DarkWake или полное пробуждение)", slept)
        last_wall, last_mono = wall, mono


async def post_init(application: Application):
    application.create_task(wake_watcher())


def main():
    try:
        instagram.start()  # при необходимости спросит логин/пароль в терминале
    except Exception as e:
        sys.exit(f"Не удалось войти в Instagram: {e}")

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    log.info("Бот запущен (лайк в Instagram: %s)", "вкл" if IG_LIKE else "выкл")
    app.run_polling()


if __name__ == "__main__":
    main()
