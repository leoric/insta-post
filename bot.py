#!/usr/bin/env python3
"""Telegram-бот: ссылка на пост Instagram + текст -> медиа в канал, реакция в телеге, лайк в инсте.

Вход в Instagram бот делает сам при запуске (без входа Instagram часто отдаёт 401/403):
  1. берёт сохранённую сессию Instaloader (~/.config/instaloader/session-<логин>);
  2. если её нет или она невалидна и задан IG_BROWSER — импортирует cookies из браузера;
  3. иначе спрашивает логин и пароль в терминале (getpass). Пароль нигде не хранится,
     сохраняется только сессия. Пустой пароль — пропустить и работать без входа.
  python3 bot.py --relogin — не использовать сохранённую сессию, войти заново.

Порядок скачивания каждой ссылки:
  1. мобильный API Instagram (видео, фото, карусели; нужен вход);
  2. yt-dlp без cookies (только одиночные видео, вход не нужен);
  3. yt-dlp с cookies (сессия бота, затем cookies браузера из IG_BROWSER);
  4. веб-метаданные Instaloader — последний вариант.
Промежуточные неудачи пишутся в лог как INFO, итог — строкой «Скачано через ...»;
WARNING/ERROR только если не сработал ни один способ.

Лайк в Instagram (IG_LIKE=true) делает instagrapi с sessionid из сессии Instaloader.
"""
import asyncio
import getpass
import logging
import os
import re
import shutil
import sys
import tempfile
import time
import traceback
from pathlib import Path

import instaloader
from dotenv import load_dotenv
from instaloader.instaloader import get_default_session_filename
from telegram import InputMediaPhoto, InputMediaVideo, LinkPreviewOptions, ReactionTypeEmoji, Update
from telegram.error import BadRequest, NetworkError
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

try:
    import yt_dlp
except ImportError:  # необязательно, но с ним рилсы качаются быстрее и без входа
    yt_dlp = None

load_dotenv(Path(__file__).resolve().parent / ".env")

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
IG_USERNAME = os.environ.get("IG_USERNAME", "").strip() or None
# Браузер, из которого брать cookies Instagram при старте (firefox, chrome...). Нужен browser_cookie3
IG_BROWSER = os.environ.get("IG_BROWSER", "").strip() or None
IG_LIKE = env_flag("IG_LIKE")  # лайк в Instagram
REACTION_EMOJI = os.getenv("REACTION_EMOJI", "😁")  # реакция бота на пост в канале
PENDING_TIMEOUT = float(os.getenv("PENDING_TIMEOUT", "60"))  # сколько ждать текст после ссылки, с
DOWNLOAD_TIMEOUT = float(os.getenv("DOWNLOAD_TIMEOUT", "180"))  # секунд на одну попытку скачивания
MAX_ATTEMPTS = 3  # попыток на ссылку, если Instagram отказал
RETRY_DELAYS = (5, 20)  # паузы перед 2-й и 3-й попыткой, секунд
PAUSE_BETWEEN_POSTS = 3  # пауза после каждой ссылки, секунд (чтобы не упираться в лимиты Instagram)

IG_URL_RE = re.compile(
    r"https?://(?:www\.)?instagram\.com/(?:[\w.]+/)?(?:p|reel|reels|tv)/[\w-]+/?(?:\?\S*)?",
    re.IGNORECASE,
)
SHORTCODE_RE = re.compile(r"/(?:p|reel|reels|tv)/([\w-]+)")

# Лимиты Bot API
CAPTION_LIMIT = 1024
BOT_FILE_LIMIT = 50 * 1024 * 1024

MEDIA_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mov"}
VIDEO_SUFFIXES = {".mp4", ".mov"}
IGRAPI_SETTINGS = Path(__file__).resolve().parent / "ig_settings.json"  # устройство instagrapi (только для лайка)

# AbortDownloadException (например, "feedback_required") не наследуется от InstaloaderException
IL_ERRORS = (
    instaloader.exceptions.InstaloaderException,
    instaloader.exceptions.AbortDownloadException,
)
RETRYABLE_ERRORS = (  # после этих ошибок имеет смысл повторить через пару секунд
    instaloader.exceptions.LoginRequiredException,
    instaloader.exceptions.BadResponseException,
)
IG_ERRORS = RETRYABLE_ERRORS + (instaloader.exceptions.AbortDownloadException,)


def _reason(e: BaseException) -> str:
    """Причина из ответа Instagram (message "feedback_required") или имя исключения."""
    m = re.search(r'message "([^"]+)"', str(e))
    return m.group(1) if m else type(e).__name__


def _short_error(e: BaseException) -> str:
    text = re.sub(r"\x1b\[[0-9;]*m", "", str(e)).strip()
    return (text.splitlines() or [type(e).__name__])[0][:300]


def log_error(e: BaseException) -> None:
    """Ошибка в логе тремя блоками: класс и текст исключения, трейс, краткая сводка."""
    name = type(e).__name__
    log.error("%s.%s: %s", type(e).__module__, name, e)
    log.error("Трейс:\n%s", "".join(traceback.format_exception(e)).rstrip())
    m = re.search(r'message "([^"]+)"', str(e))
    log.error("%s", f'{name}, "{m.group(1)}"' if m else name)


# ---------------------------------------------------------------------------
# Instagram: вход
# ---------------------------------------------------------------------------


class LoggingRateController(instaloader.RateController):
    """Стандартный контроллер лимитов, но с логом, когда Instaloader засыпает."""

    def sleep(self, secs: float):
        if secs > 1:
            log.warning("Instagram: лимит запросов, Instaloader ждёт %.0f с", secs)
        super().sleep(secs)


def make_loader() -> instaloader.Instaloader:
    return instaloader.Instaloader(
        dirname_pattern=tempfile.gettempdir(),  # на время скачивания подменяется папкой поста
        filename_pattern="{profile}_{shortcode}",
        download_video_thumbnails=False,
        download_geotags=False,
        download_comments=False,
        save_metadata=False,
        compress_json=False,
        post_metadata_txt_pattern="",  # описание нам не нужно: подпись пишет пользователь
        quiet=True,
        request_timeout=30,  # по умолчанию 300 с: зависший запрос блокировал бота на минуты
        max_connection_attempts=2,
        rate_controller=lambda ctx: LoggingRateController(ctx),
    )


LOADER = make_loader()
# python3 bot.py --relogin: не использовать сохранённую сессию, войти заново
FORCE_RELOGIN = "--relogin" in sys.argv


def has_session() -> bool:
    """Есть ли у бота cookies сессии Instagram. Не делает запросов: проверка через
    test_login бьёт в тот же graphql-запрос, который отвечает feedback_required."""
    return bool(LOADER.context.save_session().get("sessionid"))


def _saved_session_usernames() -> list[str]:
    session_dir = Path(get_default_session_filename("x")).parent
    return sorted(p.name[len("session-"):] for p in session_dir.glob("session-*"))


def _interactive_login(loader: instaloader.Instaloader, username: str | None) -> bool:
    if not sys.stdin.isatty():
        log.warning("Нет интерактивного терминала: ввод пароля недоступен")
        return False

    print("\nВход в Instagram (пароль нигде не сохраняется, пустой пароль — пропустить)")
    if not username:
        username = input("Instagram логин (Enter — пропустить): ").strip()
        if not username:
            return False

    for _ in range(3):
        password = getpass.getpass(f"Пароль для {username}: ")
        if not password:
            return False
        try:
            loader.login(username, password)
        except instaloader.exceptions.BadCredentialsException:
            print("Неверный пароль, попробуй ещё раз.")
            continue
        except instaloader.exceptions.TwoFactorAuthRequiredException:
            try:
                loader.two_factor_login(input("Код 2FA: ").strip())
            except instaloader.exceptions.InstaloaderException as e:
                print(f"Не удалось войти: {e}")
                return False
        except instaloader.exceptions.InstaloaderException as e:
            print(f"Не удалось войти: {e}")
            return False
        finally:
            password = ""  # пароль нигде не сохраняется
        loader.save_session_to_file()
        log.info("Вход выполнен, сессия сохранена для %s", username)
        return True
    return False


def ensure_login(loader: instaloader.Instaloader) -> bool:
    """Сессия -> cookies браузера -> логин с паролем. True, если вход выполнен."""
    username = IG_USERNAME
    if not username:
        saved = _saved_session_usernames()
        if len(saved) == 1:
            username = saved[0]

    if username and not FORCE_RELOGIN:
        try:
            loader.load_session_from_file(username)
        except FileNotFoundError:
            log.info("Сохранённой сессии для %s нет", username)
        except Exception as e:  # noqa: BLE001
            log.warning("Не удалось загрузить сессию: %s", e)
        else:
            try:
                verified = bool(loader.test_login())
            except Exception as e:  # noqa: BLE001
                log.warning("Не удалось проверить сессию: %s", e)
                verified = False
            if verified:
                log.info("Сессия Instagram для %s валидна", username)
                return True
            if has_session():
                # test_login отвечает None и при feedback_required, и при лимите, и без сети,
                # поэтому по нему сессию не выбрасываем: мобильный API и yt-dlp её используют
                log.warning(
                    "Сессия для %s загружена, но проверить её не удалось (feedback_required, "
                    "лимит или нет сети). Использую как есть; если она устарела, "
                    "запусти бота с флагом --relogin",
                    username,
                )
                return True
            log.warning("Сессия Instagram для %s невалидна", username)

    if IG_BROWSER:
        try:
            from instaloader.__main__ import import_session

            import_session(IG_BROWSER, loader, None)
            log.info(
                "Сессия Instagram взята из браузера %s (пользователь %s)",
                IG_BROWSER,
                loader.context.username,
            )
            try:
                loader.save_session_to_file()  # import_session сам файл не сохраняет
            except Exception as e:  # noqa: BLE001
                log.warning("Не удалось сохранить сессию из браузера: %s", e)
            return True
        except Exception as e:  # noqa: BLE001
            log.warning(
                "Не удалось взять сессию из браузера %s: %s. "
                "Нужен pip install browser_cookie3 и вход в Instagram в этом браузере",
                IG_BROWSER,
                e,
            )

    return _interactive_login(loader, username)


# ---------------------------------------------------------------------------
# Instagram: скачивание
# ---------------------------------------------------------------------------


def _natural_key(p: Path):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", p.name)]


def _find_files(dest: Path) -> list[Path]:
    return sorted(
        (p for p in dest.iterdir() if p.is_file() and p.suffix.lower() in MEDIA_SUFFIXES),
        key=_natural_key,
    )


def _clean(dest: Path) -> None:
    """Убирает недокачанное от предыдущей неудачной попытки, чтобы файлы разных способов не смешались."""
    for p in dest.iterdir():
        if p.is_file():
            p.unlink(missing_ok=True)


def _download_via_mobile_api(shortcode: str, dest: Path) -> None:
    """Мобильный API Instagram: видео, фото и карусели; нужен вход."""
    mediaid = instaloader.Post.shortcode_to_mediaid(shortcode)
    resp = LOADER.context.get_iphone_json(path=f"api/v1/media/{mediaid}/info/", params={})
    items = resp.get("items") or []
    if not items:
        raise instaloader.exceptions.BadResponseException("Мобильный API не вернул пост")
    item = items[0]
    nodes = item.get("carousel_media") or [item]

    saved = 0
    for i, node in enumerate(nodes, start=1):
        video = (node.get("video_versions") or [None])[0]
        if video:
            url, ext = video["url"], "mp4"
        else:
            candidates = (node.get("image_versions2") or {}).get("candidates") or []
            if not candidates:
                continue
            url, ext = candidates[0]["url"], "jpg"
        name = f"{shortcode}.{ext}" if len(nodes) == 1 else f"{shortcode}_{i}.{ext}"
        t0 = time.monotonic()
        LOADER.context.get_and_write_raw(url, str(dest / name))
        size = (dest / name).stat().st_size / 1048576
        log.info("[%s]   файл %s скачан: %.1f МБ за %.1f с", shortcode, name, size, time.monotonic() - t0)
        saved += 1
    if not saved:
        raise instaloader.exceptions.BadResponseException("В ответе мобильного API нет медиа")


class _YtDlpQuietLogger:
    """Глушит собственный вывод yt-dlp: ошибки мы пишем в лог сами."""

    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


class _NotAVideo(Exception):
    """Пост — фото или карусель: yt-dlp не подходит, нужен Instaloader."""


def _write_session_cookies() -> Path | None:
    """Cookies сессии бота во временном файле (формат Netscape) для yt-dlp."""
    cookies = LOADER.context.save_session()
    if not cookies.get("sessionid"):
        return None
    expires = str(int(time.time()) + 365 * 24 * 3600)
    lines = ["# Netscape HTTP Cookie File"]
    lines += ["\t".join([".instagram.com", "TRUE", "/", "TRUE", expires, k, str(v)]) for k, v in cookies.items()]
    fd, name = tempfile.mkstemp(prefix="igcookies_", suffix=".txt")  # права 0600
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return Path(name)


def _ytdlp_fetch(url: str, shortcode: str, extra_opts: dict, dest: Path) -> None:
    """Скачивает одиночное видео в <dest>/<shortcode>.mp4."""
    safe_dir = str(dest).replace("%", "%%")
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "logger": _YtDlpQuietLogger(),
        "socket_timeout": 30,
        "retries": 2,
        "noplaylist": True,
        # готовый файл со звуком; склейка дорожек (нужен ffmpeg) — только запасной вариант
        "format": "b/bv*+ba",
        "merge_output_format": "mp4",
        "outtmpl": {"default": f"{safe_dir}/{shortcode}.%(ext)s"},
        **extra_opts,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        if info.get("_type") == "playlist" or info.get("entries"):
            raise _NotAVideo("карусель")  # в ней могут быть фото, которые yt-dlp пропустит
        ydl.process_ie_result(info, download=True)


def _ytdlp_download(shortcode: str, dest: Path, failures: list[str]) -> str | None:
    """yt-dlp: сначала без cookies, при ошибке с cookies.
    Возвращает название успешной попытки или None. Неудачи пишутся в INFO и в failures:
    итог (успех или провал) оценивается только по результату всей цепочки."""
    if yt_dlp is None:
        return None
    url = f"https://www.instagram.com/p/{shortcode}/"
    attempts = [("без cookies", "none")]
    if has_session():
        attempts.append(("с cookies сессии бота", "session"))
    if IG_BROWSER:
        attempts.append((f"с cookies {IG_BROWSER}", "browser"))

    for label, kind in attempts:
        cookie_file = None
        _clean(dest)
        try:
            extra: dict = {}
            if kind == "session":
                cookie_file = _write_session_cookies()
                if cookie_file is None:
                    continue
                extra = {"cookiefile": str(cookie_file)}
            elif kind == "browser":
                extra = {"cookiesfrombrowser": (IG_BROWSER,)}
            _ytdlp_fetch(url, shortcode, extra, dest)
            if not _find_files(dest):
                raise RuntimeError("yt-dlp не создал файл")
            return f"yt-dlp ({label})"
        except _NotAVideo:
            log.info("[%s] yt-dlp: это карусель, пропускаю", shortcode)
            return None
        except Exception as e:  # noqa: BLE001
            msg = _short_error(e)
            if "no video in this post" in msg.lower():
                log.info("[%s] yt-dlp: в посте нет видео, пропускаю", shortcode)
                return None
            log.info("[%s] yt-dlp (%s): не сработал: %s", shortcode, label, msg)
            failures.append(f"yt-dlp ({label}): {msg}")
        finally:
            if cookie_file is not None:
                cookie_file.unlink(missing_ok=True)
    return None


def download_post(shortcode: str, dest: Path) -> tuple[list[Path], bool]:
    """Блокирующая загрузка в папку dest; вызывается через asyncio.to_thread.
    Возвращает (файлы, были_ли_ошибки_Instagram_при_загрузке).

    Порядок: мобильный API -> yt-dlp (без cookies, затем с cookies) -> веб-метаданные Instaloader.
    Промежуточные неудачи пишутся в INFO; WARNING/ERROR только если не сработал ни один способ."""
    failures: list[str] = []

    def done(method: str, had_error: bool = False) -> tuple[list[Path], bool]:
        files = _find_files(dest)
        log.info("[%s] Скачано через %s: %s", shortcode, method, ", ".join(p.name for p in files) or "файлов нет")
        return files, had_error

    def failed(method: str, e: BaseException) -> None:
        msg = _short_error(e)
        log.info("[%s] %s: не сработал: %s", shortcode, method, msg)
        failures.append(f"{method}: {msg}")

    def give_up() -> None:
        log.warning("[%s] Ни один способ не сработал: %s", shortcode, "; ".join(failures))

    _clean(dest)

    # 1. Мобильный API: отдаёт видео, фото и карусели; нужен вход
    if has_session():
        log.info("[%s] Instagram: пробую мобильный API", shortcode)
        try:
            _download_via_mobile_api(shortcode, dest)
            return done("мобильный API")
        except IL_ERRORS as e:
            failed("мобильный API", e)
    else:
        log.info("[%s] Нет сессии Instagram — мобильный API пропускаю", shortcode)

    # 2-3. yt-dlp: без cookies, затем с cookies (только одиночные видео)
    if yt_dlp is not None:
        log.info("[%s] Instagram: пробую yt-dlp", shortcode)
    method = _ytdlp_download(shortcode, dest, failures)
    if method:
        return done(method)

    # 4. Веб-метаданные Instaloader: последний вариант
    log.info("[%s] Instagram: пробую веб-метаданные Instaloader", shortcode)
    _clean(dest)
    try:
        post = instaloader.Post.from_shortcode(LOADER.context, shortcode)
    except IG_ERRORS as e:
        failed("Instaloader (веб)", e)
        give_up()
        raise

    had_error = False
    LOADER.dirname_pattern = str(dest).replace("{", "{{").replace("}", "}}")
    try:
        LOADER.download_post(post, target=shortcode)
    except IG_ERRORS as e:
        # Часть медиа могла скачаться, а упал дополнительный запрос.
        # Если файлы лежат на диске, считаем загрузку состоявшейся, но предупреждаем.
        log.warning("[%s] Instaloader вернул ошибку при загрузке: %s", shortcode, e, exc_info=True)
        if not _find_files(dest):
            failures.append(f"Instaloader (веб): {_short_error(e)}")
            give_up()
            raise
        had_error = True
    return done("Instaloader (веб)", had_error)


# ---------------------------------------------------------------------------
# Instagram: лайк (instagrapi, подключается только при IG_LIKE=true)
# ---------------------------------------------------------------------------

_ig = None


def _get_instagrapi():
    global _ig
    if _ig is None:
        session_id = LOADER.context.save_session().get("sessionid")
        if not session_id:
            raise RuntimeError("для лайка нужен вход в Instagram (нет сессии)")
        from instagrapi import Client

        cl = Client()
        cl.delay_range = [1, 3]
        if IGRAPI_SETTINGS.exists():
            cl.load_settings(IGRAPI_SETTINGS)
        cl.login_by_sessionid(session_id)
        cl.dump_settings(IGRAPI_SETTINGS)
        _ig = cl
    return _ig


def like_post(shortcode: str) -> bool:
    global _ig
    try:
        cl = _get_instagrapi()
        return cl.media_like(cl.media_id(cl.media_pk_from_code(shortcode)))
    except Exception:
        _ig = None  # при следующем лайке войдём заново
        raise


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------


async def post_to_channel(context: ContextTypes.DEFAULT_TYPE, files, text: str):
    """files: [(kind, path)], kind — 'photo' или 'video'."""
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
publish_lock = asyncio.Lock()  # один пост за раз (Instaloader не потокобезопасен)


async def download_with_retries(shortcode: str, dest: Path, status):
    """Скачивание с повтором, если Instagram отказал (LoginRequired / BadResponse)."""
    for attempt in range(MAX_ATTEMPTS):
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(download_post, shortcode, dest), timeout=DOWNLOAD_TIMEOUT
            )
        except asyncio.TimeoutError:
            raise RuntimeError(f"Instagram не ответил за {DOWNLOAD_TIMEOUT:.0f} с, попробуй позже") from None
        except RETRYABLE_ERRORS as e:
            if attempt == MAX_ATTEMPTS - 1:
                raise
            delay = RETRY_DELAYS[attempt]
            log.warning(
                "[%s] Попытка %d/%d не удалась (%s), повтор через %d с",
                shortcode, attempt + 1, MAX_ATTEMPTS, _short_error(e), delay,
            )
            await safe_edit(
                status, f"Instagram отказал, повторяю через {delay} с (попытка {attempt + 2}/{MAX_ATTEMPTS})…"
            )
            await asyncio.sleep(delay)


def describe_error(e: BaseException) -> str:
    """Текст ошибки для чата."""
    if isinstance(e, IG_ERRORS):
        log_error(e)
        if "feedback_required" in str(e):
            hint = "Instagram временно ограничил запросы с аккаунта бота: подожди немного и попробуй позже."
        elif has_session():
            hint = "Сессия, возможно, устарела: перезапусти бота командой python3 bot.py --relogin."
        else:
            hint = "Бот работает без входа в Instagram: перезапусти его в терминале и введи логин и пароль."
        return f"Instagram не отдал пост ({_reason(e)}). {hint}"
    if isinstance(e, IL_ERRORS):
        log_error(e)
        return f"Не получилось скачать: {_short_error(e)}"
    log_error(e)
    return _short_error(e)


async def process_post(context: ContextTypes.DEFAULT_TYPE, status, url: str, text: str):
    """Скачать -> опубликовать в канал -> реакция -> лайк. status — сообщение, которое обновляем."""
    code = SHORTCODE_RE.search(url).group(1)
    t0 = time.monotonic()
    async with publish_lock:
        log.info("[%s] ▶ Старт. Текст: %d симв.", code, len(text))
        tmp = Path(tempfile.mkdtemp(prefix="igbot_"))
        try:
            await safe_edit(status, "⏳ Скачиваю…")
            paths, had_error = await download_with_retries(code, tmp, status)
            if not paths:
                raise RuntimeError("Загрузка завершилась, но файлов не найдено")
            files = [("video" if p.suffix.lower() in VIDEO_SUFFIXES else "photo", p) for p in paths]
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
                    liked = await asyncio.wait_for(asyncio.to_thread(like_post, code), timeout=DOWNLOAD_TIMEOUT)
                    log.info("[%s] Instagram: лайк %s", code, "поставлен" if liked else "не поставился")
                    notes.append("❤️ лайк в инсте поставлен" if liked else "⚠️ лайк в инсте не поставился")
                except Exception as e:
                    log.exception("[%s] Instagram: ошибка лайка", code)
                    notes.append(f"⚠️ лайк в инсте не поставился: {_short_error(e)}")

            if had_error:
                notes.append("⚠️ Instagram вернул ошибку при загрузке: часть файлов могла не скачаться")
            count = f" ({len(files)} шт.)" if len(files) > 1 else ""
            await safe_edit(status, f"✅ Опубликовано{count}\n" + "\n".join(notes))
            log.info("[%s] ✅ Готово за %.1f с", code, time.monotonic() - t0)
            await asyncio.sleep(PAUSE_BETWEEN_POSTS)
        except Exception as e:
            log.error("[%s] ❌ Ошибка после %.1f с", code, time.monotonic() - t0)
            err = describe_error(e) or type(e).__name__
            if len(err) > 400:
                err = err[:400] + "…"
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
    if not msg or not msg.text or not user:
        return
    if user.id not in ALLOWED_USERS:
        log.warning("Сообщение от неразрешённого пользователя id=%s (в .env ALLOWED_USER_IDS: %s)", user.id, sorted(ALLOWED_USERS))
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


async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает Telegram ID, чтобы вписать его в ALLOWED_USER_IDS."""
    if update.effective_message and update.effective_user:
        await update.effective_message.reply_text(f"Твой Telegram ID: {update.effective_user.id}")


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Сетевые сбои — одной строкой, остальное — с traceback."""
    err = context.error
    if isinstance(err, NetworkError):
        log.warning("Проблема с сетью, Telegram-клиент переподключится сам: %s", err)
    else:
        log.error("Необработанная ошибка", exc_info=err)


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
    ensure_login(LOADER)
    if not has_session():
        log.warning("Работаю без входа в Instagram: возможны ошибки 401/403%s", "; лайк работать не будет" if IG_LIKE else "")
    if yt_dlp is None:
        log.info("yt-dlp не установлен: pip install yt-dlp (рилсы будут качаться быстрее и без входа)")

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_error_handler(on_error)
    app.add_handler(CommandHandler("start", handle_start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    log.info("Бот запущен (лайк в Instagram: %s)", "вкл" if IG_LIKE else "выкл")
    # Сообщения, отправленные пока бот был выключен, не отбрасываем
    app.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    main()
