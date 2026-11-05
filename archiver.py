#!/usr/bin/env python3
"""INACAP virtual-campus archiver.

Discovers the Digital Resources ("Recursos Digitales") listed in the user's
Moodle courses, then downloads new ones (text + media + attached PDFs) into a
local tree. Pure HTTP for the downloads; a headless browser is used only to log
in. Designed to run unattended on a daily schedule: with INACAP_USER/INACAP_PASS
in .env it obtains and renews the Moodle session by itself, with no manual step.

Auth map (verified):
  - Discovery lives on Moodle (aai.inacap.cl), behind an SSO session cookie
    (MoodleSession). Course pages are server-rendered HTML.
  - Each resource is a Moodle /mod/url redirector; following it with
    &redirect=1 yields the real package URL on virtual.inacap.cl carrying a
    per-user `sci` token. The package's static assets need no auth.
  - The `sci` token is handed out fresh by the redirect on every run, so we
    never store or reverse-engineer it.

Usage:
  python3 archiver.py --discover      # list resources, resolve URLs, no download
  python3 archiver.py                  # discover + download new resources
  python3 archiver.py --setup         # credentials, then optional Telegram/NotebookLM
  python3 archiver.py --install-schedule  # register the daily 08:00 run
  python3 archiver.py --bot            # serve Telegram commands (long-poll)
  python3 archiver.py --uninstall      # remove schedules, bot and personal data
  python3 archiver.py --self-test      # run offline parser checks
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import datetime as dt
import html
import json
import os
import pathlib
import re
import sys
import time
import types
import urllib.parse

ROOT = pathlib.Path(__file__).parent
ARCHIVE = ROOT / "archive"
MANIFEST = ROOT / "manifest.json"
LOCK = ROOT / ".lock"

# Enrolled courses are discovered automatically from Moodle. This list is an
# optional floor: add a course id here to force-archive one the service misses.
COURSES: list[int] = []

MOODLE = "https://aai.inacap.cl"
REPO_HOST = "virtual.inacap.cl"

# Skip decorative media larger than this. INACAP ships 1080p animated GIFs of
# ~149 MB as "videos"; not worth mirroring daily. PDFs/PPTX/text are unaffected
# in practice. Raise it to keep everything.
MAX_ASSET_MB = 50
# Generous on purpose: the first sync of a term's backlog took 11 minutes. What
# this bounds is not slowness, it is a stall — see sync_to_notebooklm.
NOTEBOOKLM_TIMEOUT = 30 * 60

# Optional Google Drive sync, configured per-install in .env as
#   DRIVE_REMOTE=gdrive:INACAP
# after running `rclone config` once; empty (the default) disables it. It lives
# in .env, not here, so a fresh clone never syncs to whoever published the repo.
# rclone handles OAuth, token refresh and incremental upload. See the README.


def _oversize(head, url: str) -> bool:
    """True if a HEAD says the asset exceeds MAX_ASSET_MB (so skip the GET)."""
    try:
        h = head(url, timeout=30, allow_redirects=True)
        return int(h.headers.get("content-length", 0)) > MAX_ASSET_MB * 1024 * 1024
    except Exception:
        return False  # unknown size — let the normal download attempt proceed

# Course activities we archive: url (Rise/Storyline packages on virtual),
# resource (a single uploaded file), folder (a bundle of uploaded files),
# assign (a taller: the teacher's brief plus our own handed-in file).
# quiz/forum are skipped — a quiz has nothing to fetch until it is attempted,
# and mod/quiz is deliberately left alone (see the warning on _ARCHIVED_KINDS).
#
# WARNING: never add "quiz" here without reading mod/quiz's attempt flow first.
# These quizzes allow a single attempt on a timer, and starting one is a POST to
# startattempt.php that the student cannot undo. Only review.php, only by GET,
# and only when an attempt already exists, is ever safe to touch.
_ARCHIVED_KINDS = ("url", "resource", "folder", "assign")

# One activity in the course HTML: a /mod/<kind> link wrapping an instancename.
_RESOURCE_RE = re.compile(
    r'mod/(?P<kind>url|resource|folder|assign)/view\.php\?id=(?P<id>\d+)[^>]*>\s*'
    r'<span class="instancename">\s*(?P<name>.*?)\s*(?:<span|</span>)',
    re.IGNORECASE | re.DOTALL,
)


def parse_resources(html: str) -> list[tuple[str, str, str]]:
    """Extract (moodle_id, kind, name) for every archivable activity."""
    seen: dict[str, tuple[str, str]] = {}
    for m in _RESOURCE_RE.finditer(html):
        name = re.sub(r"\s+", " ", m.group("name")).strip()
        seen.setdefault(m.group("id"), (m.group("kind").lower(), name))
    return [(rid, kind, name) for rid, (kind, name) in seen.items()]


def _safe_name(name: str) -> str:
    """A filesystem-safe folder name from a human course/activity title."""
    return re.sub(r"\s+", " ", re.sub(r'[/\\:*?"<>|]', "-", name)).strip() or "sin-nombre"


def repo_parts(url: str) -> tuple[str, str, str] | None:
    """Map a virtual.inacap.cl package URL to (course, unit, resource) folders.

    e.g. .../repositorio/TI3061_ASP/U1/TI3061_U1_S1_RD/content/index.php
         -> ("TI3061_ASP", "U1", "TI3061_U1_S1_RD")
    """
    m = re.search(r"/repositorio/([^/]+)/([^/]+)/([^/]+)/", url)
    return (m.group(1), m.group(2), m.group(3)) if m else None


def load_cookie(curlrc: str) -> str:
    """Read the `cookie = "..."` line from a gitignored curlrc file.

    Raises SessionExpired rather than exiting: on a fresh install this file does
    not exist yet, and the auto-login is what creates it. Killing the process
    here would send a new user to DevTools for a cookie the tool can fetch itself.
    """
    path = ROOT / curlrc
    if not path.exists():
        raise SessionExpired(f"{curlrc} todavía no existe")
    m = re.search(r'cookie\s*=\s*"(.*)"', path.read_text(encoding="utf-8"))
    if not m:
        raise SessionExpired(f"{curlrc} no tiene una línea cookie = \"...\"")
    return m.group(1)


USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)


class SessionExpired(Exception):
    """The Moodle cookie no longer authenticates."""


class Busy(Exception):
    """Another run already holds the lock."""


@contextlib.contextmanager
def single_run():
    """Serialize runs, so a /bajar from the bot can't race the daily cron.

    Two concurrent runs would download the same resource twice and interleave
    their writes to manifest.json, losing entries.

    Both branches take an OS-level lock on an open handle, so the lock dies with
    the process — a crashed run never leaves the next one shut out. There is no
    portable stdlib call for this, hence the split: fcntl on POSIX, msvcrt on
    Windows.
    """
    with open(LOCK, "w", encoding="utf-8") as fh:
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise Busy("another run is in progress")
        yield


def make_session():
    """A session carrying the Moodle cookie, fetching one on a fresh install."""
    import requests

    try:
        cookie = load_cookie("aai.curlrc")
    except SessionExpired:
        # No usable cookie yet. refresh_cookie() writes one from the credentials
        # in .env, so try that before sending anyone to hunt through DevTools.
        if not refresh_cookie():
            sys.exit(
                "Todavía no hay sesión de Moodle. Configura tus credenciales con "
                f"{_cmd()} --setup (recomendado), o exporta la cookie a "
                "aai.curlrc. Ver README."
            )
        cookie = load_cookie("aai.curlrc")

    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    s.headers["Cookie"] = cookie
    return s


def _parse_env(text: str) -> dict:
    """Parse KEY=VALUE lines (no dependency needed).

    Only one matching pair of surrounding quotes is dropped, so a quoted value
    keeps quotes and edge spaces of its own — passwords can have both.
    """
    env = {}
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            env[k.strip()] = v
    return env


def _load_env() -> dict:
    """The gitignored .env, parsed."""
    path = ROOT / ".env"
    return _parse_env(path.read_text(encoding="utf-8")) if path.exists() else {}


def _command_name(launcher_text: str | None, root: str) -> str:
    """How hints should tell the student to run this program.

    `inacap` exists only when an installer wrote a launcher for this very
    folder; a manual install or a git checkout runs the script itself.
    """
    if not launcher_text:
        return "python3 archiver.py"
    # The .cmd launcher says %USERPROFILE%; expand it like cmd.exe would.
    text = re.sub(r"%(\w+)%", lambda m: os.environ.get(m.group(1), m.group(0)), launcher_text)
    if os.path.normcase(root) in os.path.normcase(text):
        return "inacap"
    return "python3 archiver.py"


def _cmd() -> str:
    """_command_name for this install."""
    launcher = _launcher(pathlib.Path.home(), os.name == "nt", os.environ.get("XDG_BIN_HOME", ""))
    try:
        text = launcher.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = None
    return _command_name(text, str(ROOT.resolve()))


def _with_env(current: str | None, template: str, values: dict) -> str:
    """The .env text with `values` set and every other line kept.

    `current` is None when there is no .env yet; then the template is the start.
    Values are always double-quoted, which _parse_env undoes exactly.
    """
    out, written = [], set()
    for line in (template if current is None else current).splitlines():
        key = line.split("=", 1)[0].strip()
        if "=" in line and key in values:
            line = f'{key}="{values[key]}"'
            written.add(key)
        out.append(line)
    out += [f'{k}="{v}"' for k, v in values.items() if k not in written]
    return "\n".join(out) + "\n"


def _save_env(values: dict) -> None:
    """Set these keys in the .env (created from .env.example if missing)."""
    env_path, example = ROOT / ".env", ROOT / ".env.example"
    template = example.read_text(encoding="utf-8") if example.exists() else ""
    current = env_path.read_text(encoding="utf-8") if env_path.exists() else None
    env_path.write_text(_with_env(current, template, values), encoding="utf-8")
    env_path.chmod(0o600)


def _yes(prompt: str, default: bool = True) -> bool:
    answer = input(prompt).strip().lower()
    return answer in ("s", "si", "sí") or (default and not answer)


def setup() -> None:
    """Ask for the INACAP credentials, save them to .env and try them out.

    Then offer the optional extras, one by one.
    """
    import getpass

    example = ROOT / ".env.example"
    template = example.read_text(encoding="utf-8") if example.exists() else ""
    env = _load_env()
    user = env.get("INACAP_USER", "")
    # Re-running the installer must not force retyping a password that works,
    # but it is also how a student changes one, so the choice is theirs.
    keep = bool(user and env.get("INACAP_PASS")
                and user != _parse_env(template).get("INACAP_USER")
                and _yes(f"Ya hay credenciales guardadas para {_mask(user)}. "
                         "¿Mantenerlas? [S/n] (n = cambiarlas) "))
    while True:
        if not keep:
            print("Ingresa tus datos de INACAP. Se guardan solo en este equipo (archivo .env).")
            user = input("Usuario (correo @inacapmail.cl o RUT): ").strip()
            password = getpass.getpass("Contraseña (no se ve mientras escribes): ")
            if not user or not password:
                print("Faltó el usuario o la contraseña.\n")
                continue
            _save_env({"INACAP_USER": user, "INACAP_PASS": password})
        try:
            ok = refresh_cookie()
        except Exception as e:  # no network, INACAP's page changed, ...
            print(f"No se pudo conectar con INACAP ({type(e).__name__}: {e}).")
            ok = False
        if ok:
            print("✓ Inicio de sesión correcto. Credenciales guardadas.")
            break
        print("✗ No se pudo iniciar sesión. Revisa el usuario y la contraseña.")
        if not _yes("¿Intentar de nuevo? [S/n] "):
            sys.exit(f"Sin credenciales válidas. Para reintentar: {_cmd()} --setup")
        keep = False

    print(f"\nExtras opcionales (puedes activarlos después con: {_cmd()} --setup)")
    for extra in (_setup_telegram, _setup_notebooklm):
        print()
        try:
            extra()
        except Exception as e:  # one extra failing must not cost the others
            print(f"✗ No se pudo completar ({type(e).__name__}: {_redact(str(e))}). "
                  f"Puedes reintentarlo con: {_cmd()} --setup")


def _offer(question: str, configured: str) -> bool:
    """Ask before an optional extra; default no. Re-runs offer to redo it."""
    if configured:
        print(configured)
        return _yes("¿Configurarlo de nuevo? [s/N] ", default=False)
    return _yes(f"{question} [s/N] ", default=False)


def _setup_telegram() -> None:
    """Create-a-bot walkthrough: token, chat id, test message, bot service."""
    conf = _telegram_conf()
    if not _offer("¿Quieres avisos de material nuevo y pedir descargas por Telegram?",
                  conf and "Telegram ya está configurado."):
        return
    print("Crea tu bot (toma un minuto):\n"
          "  1. En Telegram, busca a @BotFather y abre la conversación.\n"
          "  2. Envíale /newbot y responde lo que pregunta: un nombre y un usuario\n"
          "     que termine en \"bot\" (por ejemplo, ana_inacap_bot).\n"
          "  3. Te responde con un token parecido a 123456789:AAH...; cópialo.")
    while True:
        token = input("Pega aquí el token (Enter para omitir): ").strip()
        if not token:
            return
        me = _telegram_call(token, "getMe")
        if me.get("ok"):
            break
        print(f"Telegram no aceptó ese token ({me.get('description')}). Revisa que esté completo.")
    bot_name = me["result"].get("username")
    # A running bot polls getUpdates too, and would swallow the message we need.
    # Whatever happens next (cancel, error, Ctrl-C), a bot that was running is
    # put back; after a successful setup it starts with the new settings.
    was_running = _bot_service(start=False)
    done = False
    try:
        chat = ""
        while not chat:
            if input(f"Ahora escríbele cualquier mensaje a @{bot_name} en Telegram y luego "
                     "presiona Enter aquí (o escribe salir): ").strip().lower() == "salir":
                print("Telegram no se configuró.")
                return
            # Newest first: on a reused bot, an old chat may still be in the queue.
            for chat_id, name, text in reversed(
                    _private_chats(_telegram_call(token, "getUpdates"))):
                if _yes(f"¿Eres tú, {name}, que escribiste «{text}»? [s/N] ", default=False):
                    chat = chat_id
                    break
            else:
                print("Todavía no llega tu mensaje. Revisa que se lo hayas enviado a "
                      f"@{bot_name} desde tu chat personal y vuelve a intentarlo.")
        _save_env({"TELEGRAM_TOKEN": token, "TELEGRAM_CHAT_ID": chat})
        done = True
        sent = _telegram_call(token, "sendMessage", {
            "chat_id": chat,
            "text": "✓ INACAP Archiver conectado. Aquí te avisaré del material nuevo."})
        if sent.get("ok"):
            print("✓ Telegram listo: te envié un mensaje de prueba.")
        else:
            print("✗ Telegram quedó guardado, pero no aceptó el mensaje de prueba "
                  f"({sent.get('description')}).")
    finally:
        if done or was_running:
            _bot_service(start=True)


def _notebooklm_home(home: pathlib.Path | None = None) -> pathlib.Path:
    """Where notebooklm-py keeps the Google login (its paths.get_home_dir)."""
    return pathlib.Path(os.environ.get("NOTEBOOKLM_HOME")
                        or (home or pathlib.Path.home()) / ".notebooklm").expanduser()


def _setup_notebooklm() -> None:
    """Install the notebooklm CLI into this Python and run its login."""
    import shutil
    import subprocess

    cli = _notebooklm_cli(os.environ.get("PATH", ""), sys.executable)
    logged_in = cli and any(_notebooklm_home().rglob("storage_state.json"))
    if not _offer("¿Quieres subir tu material a NotebookLM (un cuaderno por ramo)?",
                  logged_in and "NotebookLM ya está configurado."):
        return
    if not cli:
        print("Instalando notebooklm-py ...", flush=True)
        package = "notebooklm-py[browser]"
        # Same places the installer looks: uv may not be on this PATH yet.
        uv = shutil.which("uv", path=os.pathsep.join(
            p for p in (os.environ.get("XDG_BIN_HOME"), str(pathlib.Path.home() / ".local/bin"),
                        _tool_path(os.environ.get("PATH", ""))) if p))
        # A uv-made venv has no pip, so pip is only the fallback for no uv at all.
        if uv:
            rc = subprocess.run([uv, "pip", "install", "--python", sys.executable,
                                 package]).returncode
            if rc != 0:
                raise RuntimeError(f"uv no pudo instalar notebooklm-py (salió con {rc}); "
                                   "revisa el mensaje de arriba")
        else:
            subprocess.run([sys.executable, "-m", "pip", "install", package], check=True)
        cli = _notebooklm_cli(os.environ.get("PATH", ""), sys.executable)
        if not cli:
            raise RuntimeError("se instaló notebooklm-py pero no encuentro su CLI")
    print("Se abrirá un navegador: inicia sesión con tu cuenta de Google y espera\n"
          "a que cargue NotebookLM.")
    subprocess.run([cli, "login"], check=True)
    print("✓ NotebookLM listo: cada material nuevo se sube solo.")


# Moodle cookies worth persisting; the rest (analytics) are dropped.
_KEEP_COOKIE = re.compile(r"^(MoodleSession|MDL_|BIGipServerMOODLE|MOODLEID)", re.I)


def refresh_cookie() -> bool:
    """Log in through the ADFS form in a headless browser, save the cookie.

    ponytail: the browser handles the whole SAML/ADFS redirect dance that would
    be brutally fragile to replay by hand. Credentials come from a gitignored
    .env and are sent only to INACAP's own login form.
    """
    env = _load_env()
    user, pw = env.get("INACAP_USER"), env.get("INACAP_PASS")
    if not user or not pw:
        return False  # no credentials configured — caller falls back to manual

    from playwright.sync_api import sync_playwright

    print("Renovando la sesión de Moodle (inicio automático) ...", flush=True)
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="chrome", headless=True)
        except Exception:
            # channel="chrome" needs Google Chrome itself, which a Linux box
            # often lacks — and launchd/systemd may not find it even when it is
            # installed. Fall back to Playwright's own build.
            try:
                browser = p.chromium.launch(headless=True)
            except Exception as e:
                # Both browsers are missing. Return False like any other login
                # failure: raising here would abort the whole run with a
                # traceback instead of the caller's own error handling.
                print(f"El inicio automático no encontró un navegador usable "
                      f"({e.__class__.__name__}).\n"
                      "  Instala uno con: python3 -m playwright install chromium",
                      flush=True)
                return False
        page = browser.new_context(user_agent=USER_AGENT).new_page()
        page.goto(f"{MOODLE}/my/", wait_until="domcontentloaded", timeout=45000)
        page.fill("#userNameInput", user)
        page.fill("#passwordInput", pw)
        page.click("#submitButton")
        try:
            page.wait_for_url(re.compile(r"https://aai\.inacap\.cl/"), timeout=45000)
        except Exception:
            browser.close()
            return False  # login did not land back on Moodle (wrong creds?)
        cookies = page.context.cookies("https://aai.inacap.cl")
        browser.close()

    jar = {c["name"]: c["value"] for c in cookies if _KEEP_COOKIE.match(c["name"])}
    if "MoodleSession" not in jar:
        return False
    cookie_line = "; ".join(f"{k}={v}" for k, v in jar.items())
    curlrc = ROOT / "aai.curlrc"
    curlrc.write_text(
        "#Moodle session for aai.inacap.cl. Auto-refreshed by archiver.py.\n"
        f'user-agent = "{USER_AGENT}"\n'
        f'cookie = "{cookie_line}"\n'
    )
    curlrc.chmod(0o600)
    return True


def _is_login(response) -> bool:
    """True if the response is the SSO login page, not the requested content."""
    return "adfs" in response.url or "loginform" in response.text.lower()


def fetch_course(session, course_id: int) -> str | None:
    """Course HTML, or None if this course isn't accessible with the session.

    A single restricted course (some /my/ cards redirect to SSO) must not abort
    the whole run; true session expiry is caught once, at the /my/ gate.
    """
    r = session.get(f"{MOODLE}/course/view.php?id={course_id}", timeout=40)
    r.raise_for_status()
    return None if _is_login(r) else r.text


def course_name(html: str, course_id: int) -> str:
    """Readable course name from the page title ("Curso: X | AAI" -> "X")."""
    m = re.search(r"<title>\s*(?:Curso:\s*)?(.*?)\s*(?:\|[^<]*)?</title>", html, re.I)
    return _safe_name(m.group(1)) if m and m.group(1).strip() else f"course_{course_id}"


def resolve(session, moodle_id: str) -> str | None:
    """Final URL behind a /mod/url redirector, read without leaving Moodle.

    Moodle answers with a 303 whose Location already carries the target, so the
    header is all we need. Following it would fetch a third-party host only to
    throw the page away — and not every /mod/url points at the repositorio: one
    of them is a multi-megabyte dataset tarball served over slow plain HTTP.
    Downloading it hung the whole run, because a read timeout restarts on every
    chunk that arrives, so a server that trickles bytes never trips it.
    """
    r = session.get(
        f"{MOODLE}/mod/url/view.php?id={moodle_id}&redirect=1",
        timeout=40,
        allow_redirects=False,
    )
    location = r.headers.get("Location", "")
    return location if REPO_HOST in location else None


def discover_courses(session) -> list[int]:
    """Every enrolled course, from Moodle's own enrolment web service.

    The /my/ dashboard only shows pinned/institutional cards and misses the
    academic courses, so we ask the same AJAX service the dashboard's block
    uses. /my/courses.php doubles as the session gate and the sesskey source.
    """
    r = session.get(f"{MOODLE}/my/courses.php", timeout=40)
    r.raise_for_status()
    if _is_login(r):
        raise SessionExpired
    sk = re.search(r'"sesskey":"([^"]+)"', r.text)
    if not sk:
        raise SessionExpired

    resp = session.post(
        f"{MOODLE}/lib/ajax/service.php",
        params={"sesskey": sk.group(1)},
        json=[{
            "index": 0,
            "methodname": "core_course_get_enrolled_courses_by_timeline_classification",
            "args": {"offset": 0, "limit": 0, "classification": "all",
                     "sort": "fullname"},
        }],
        timeout=40,
    )
    resp.raise_for_status()
    payload = resp.json()[0]
    if payload.get("error"):
        # Service failed — fall back to the explicit list rather than archiving nothing.
        return sorted(set(COURSES))
    enrolled = {c["id"] for c in payload["data"]["courses"]}
    return sorted(set(COURSES) | enrolled)


def discover(session) -> list[dict]:
    """Return activity records across all discovered courses."""
    out: list[dict] = []
    for course_id in discover_courses(session):
        html = fetch_course(session, course_id)
        if html is None:
            continue  # course not accessible with this session — skip, don't abort
        cname = course_name(html, course_id)
        for moodle_id, kind, name in parse_resources(html):
            rec = {
                "id": moodle_id,
                "course_id": course_id,
                "course_name": cname,
                "kind": kind,
                "name": name,
            }
            if kind == "url":
                # A package on virtual (Rise/Storyline); resolve to its real URL.
                try:
                    url = resolve(session, moodle_id)
                except Exception as e:
                    # One unreachable redirector costs one resource, not the
                    # whole run — same rule as an inaccessible course above.
                    print(f"no se pudo resolver {moodle_id} ({name}): {e}")
                    continue
                if not url:
                    continue  # external link or non-repositorio target — skip
                parts = repo_parts(url)
                rec["url"] = url
                rec["unit"] = parts[1] if parts else None
                rec["resource"] = parts[2] if parts else None
            else:
                # A Moodle file activity (resource/folder); page holds the files.
                rec["url"] = f"{MOODLE}/mod/{kind}/view.php?id={moodle_id}"
            out.append(rec)
    return out


def load_manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8")) if MANIFEST.exists() else {}


def save_manifest(manifest: dict) -> None:
    MANIFEST.write_text(json.dumps(manifest, indent=2, ensure_ascii=False),
                        encoding="utf-8")


def dest_for(rec: dict) -> pathlib.Path:
    course = rec["course_name"]
    if rec["kind"] == "url":
        # Rise/Storyline package: <course>/<unit>/<resource>/
        unit = rec.get("unit") or "U0"
        resource = rec.get("resource") or rec["id"]
        return ARCHIVE / course / unit / resource
    # Uploaded file activity: <course>/<activity name>/
    return ARCHIVE / course / _safe_name(rec["name"])


# Fields in the Rise content model that hold human-readable prose.
#
# "destination" is not prose: it is the URL of a link block, and it is here
# because dropping it lost every citation the material makes. A link block
# renders its label and description through the keys above, so the markdown
# announced "IR a la lectura" and then led nowhere. Keeping the URL also feeds
# notebooklm_sync, which mines the archived text for sources to upload.
# Deliberately absent: thumbnail, coverImageDefault, navigationOverlayImage and
# favicon are artwork, and originalUrl/providerUrl belong to an embed's player
# rather than to anything the author chose to cite.
_TEXT_KEYS = {
    "description", "title", "text", "caption", "heading", "paragraph",
    "content", "label", "altText", "body", "term", "definition",
    "question", "answer", "destination",
}
_DOC_RE = re.compile(r"\.(pdf|docx?|pptx?|xlsx?|zip)$", re.IGNORECASE)


def _strip_html(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(s))).strip()


_EMBED_SRC = re.compile(r'src="(https?://[^"]+)"', re.IGNORECASE)


def _embed_url(block: dict) -> str | None:
    """The address of an embedded player, when a block holds one.

    Videos and interactives are not part of the package: they live on Vimeo,
    Genially or INACAP's own player, so nothing can be downloaded and the
    archive would otherwise lose every trace of them. Keeping the address at
    least records that the material has a video, and where.

    Rise stores it in originalUrl, either bare or wrapped in the whole <iframe>
    the author pasted. That same field doubles as a scratch pad for asset
    filenames and image-generation prompts, which is why only a real URL counts
    — the rest would flood the markdown with noise.
    """
    raw = block.get("originalUrl")
    if not isinstance(raw, str):
        return None
    if raw.startswith(("http://", "https://")):
        return raw
    m = _EMBED_SRC.search(raw)
    return html.unescape(m.group(1)) if m else None


def _lesson_text(node) -> list[str]:
    """Collect deduped prose strings from a lesson subtree, in document order."""
    out: list[str] = []
    seen: set[str] = set()

    def walk(o, key=None):
        if isinstance(o, dict):
            embed = _embed_url(o)
            if embed and embed not in seen:
                seen.add(embed)
                out.append(embed)
            for k, v in o.items():
                walk(v, k)
        elif isinstance(o, list):
            for v in o:
                walk(v, key)
        elif isinstance(o, str) and key in _TEXT_KEYS:
            t = _strip_html(o)
            if len(t) > 3 and t not in seen:
                seen.add(t)
                out.append(t)

    walk(node)
    return out


def _render_markdown(course: dict) -> str:
    parts = [f"# {course.get('title', 'Recurso digital')}\n"]
    if course.get("description"):
        parts.append(_strip_html(course["description"]) + "\n")
    for lesson in course.get("lessons", []):
        parts.append(f"\n## {lesson.get('title', '').strip()}\n")
        # Skip the lesson title itself (already a heading) in the body text.
        body = [t for t in _lesson_text(lesson) if t != lesson.get("title", "").strip()]
        parts.extend(body)
    return "\n\n".join(parts) + "\n"


def _collect_assets(data) -> set[str]:
    """Filenames served under content/assets/: inline media + attached docs."""
    names: set[str] = set()

    def walk(o):
        if isinstance(o, dict):
            # Assets are served flat under content/assets/<basename>, so a
            # crushedKey that carries a storage path is reduced to its basename.
            if o.get("crushedKey") and o.get("useCrushedKey"):
                names.add(o["crushedKey"].split("/")[-1])
            for key in ("crushedKey", "key", "originalUrl", "url", "src", "file"):
                v = o.get(key)
                if isinstance(v, str) and _DOC_RE.search(v):
                    names.add(v.split("/")[-1])
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(data)
    return names


def download(session, rec: dict, dest: pathlib.Path) -> dict:
    """Dispatch by activity kind: Rise package vs. uploaded file(s)."""
    if rec["kind"] == "url":
        return download_rise(rec["url"], dest)
    return download_files(session, rec["url"], dest)


# Real activity files live under pluginfile.php in a mod_folder / mod_resource /
# mod_assign context, plus assignsubmission_file for what we handed in ourselves;
# the theme's own logo/favicon use core_admin and are excluded.
_PLUGINFILE_RE = re.compile(
    r'https://[^"\'<> ]*/pluginfile\.php/[^"\'<> ]*', re.IGNORECASE
)
_CONTENT_FILE_RE = re.compile(
    r"/(mod_(?:folder|resource|assign)|assignsubmission_file)/", re.IGNORECASE
)


def _content_pluginfiles(html_text: str) -> list[str]:
    """Downloadable activity files from a page: real content only, deduped."""
    by_path: dict[str, str] = {}
    for raw in _PLUGINFILE_RE.findall(html_text):
        url = html.unescape(raw)
        if not _CONTENT_FILE_RE.search(url) or "preview=" in url:
            continue  # skip theme chrome and thumbnail variants
        by_path.setdefault(url.split("?")[0], url)  # one entry per file path
    return sorted(by_path.values())


def download_files(session, activity_url: str, dest: pathlib.Path) -> dict:
    """Download the file(s) behind a mod/resource or mod/folder activity.

    mod/resource may stream the file directly; mod/folder renders a page whose
    file links are pluginfile.php URLs. Both are handled by fetching the URL and
    either saving the file body or harvesting its pluginfile links.
    """
    r = session.get(activity_url, timeout=90, allow_redirects=True)
    r.raise_for_status()
    ctype = r.headers.get("content-type", "")

    dest.mkdir(parents=True, exist_ok=True)
    saved, missing = [], []

    if not ctype.startswith("text/html"):
        # The activity streamed the file itself (typical for mod/resource).
        name = _filename_from(r)
        (dest / name).write_bytes(r.content)
        return {"files": [name], "missing": []}

    for url in _content_pluginfiles(r.text):
        if _oversize(session.head, url):
            missing.append(url)
            continue
        try:
            rr = session.get(url, timeout=120, allow_redirects=True)
            if rr.ok and rr.content and not rr.headers.get(
                "content-type", ""
            ).startswith("text/html"):
                name = _filename_from(rr)
                (dest / name).write_bytes(rr.content)
                saved.append(name)
            else:
                missing.append(url)
        except Exception:
            missing.append(url)
    return {"files": saved, "missing": missing}


def _fix_mojibake(s: str) -> str:
    """Repair UTF-8 bytes that were decoded as Latin-1 ("PrÃ¡ctico" -> "Práctico").

    Moodle serves file names as UTF-8, but HTTP headers and some HTML decode as
    Latin-1, producing mojibake. Only touched when the tell-tale bytes appear.
    """
    if any(c in s for c in "ÃÂÐÑ"):
        try:
            return s.encode("latin-1").decode("utf-8")
        except UnicodeError:
            pass
    return s


def _filename_from(response) -> str:
    """Best filename for a downloaded file: Content-Disposition, else URL tail."""
    cd = response.headers.get("content-disposition", "")
    m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)', cd)
    raw = urllib.parse.unquote(m.group(1)) if m else urllib.parse.unquote(
        response.url.split("?")[0].rstrip("/").split("/")[-1]
    )
    return _safe_name(_fix_mojibake(raw)) or "archivo"


def download_rise(url: str, dest: pathlib.Path) -> dict:
    """Extract text + media from a Rise package's runtime-data.js (no browser).

    The whole course (all lessons, prose, media refs, attached PDFs) ships as
    base64 JSON inside content/runtime-data.js. Static assets under
    content/assets/ need no auth, so this is pure HTTP — fast and cron-friendly.

    ponytail: handles Articulate Rise (content/index.php), the only RD type in
    the user's courses. Storyline packages (story.php) have no runtime-data.js;
    they're recorded as unsupported and skipped rather than crashing the run.
    """
    import requests

    content_root = url.split("?")[0].rsplit("/", 1)[0]  # .../<RES>/content
    r = requests.get(f"{content_root}/runtime-data.js", timeout=40)
    r.raise_for_status()
    m = re.match(r'__jsonp\("[^"]*","([^"]*)"\)', r.text.strip())
    if not m:
        return {"skipped": "not a Rise package (no runtime-data.js jsonp)"}

    data = json.loads(base64.b64decode(m.group(1)))
    course = data["course"]

    dest.mkdir(parents=True, exist_ok=True)
    text = _render_markdown(course)
    (dest / "content.md").write_text(text, encoding="utf-8")

    media_dir = dest / "media"
    media_dir.mkdir(exist_ok=True)
    saved, missing, oversize = [], [], []
    for name in sorted(_collect_assets(data)):
        asset_url = f"{content_root}/assets/{urllib.parse.quote(name)}"
        if _oversize(requests.head, asset_url):
            oversize.append(name)
            continue
        try:
            rr = requests.get(asset_url, timeout=90)
            if rr.ok and rr.content:
                (media_dir / name).write_bytes(rr.content)
                saved.append(name)
            else:
                missing.append(name)
        except Exception:  # one broken asset must not sink the whole resource
            missing.append(name)

    return {
        "chars": len(text),
        "media": saved,
        "missing": missing,
        "oversize": oversize,
        "lessons": len(course.get("lessons", [])),
    }


def pending(resources: list[dict], manifest: dict,
            retry_unsupported: bool = False) -> list[dict]:
    """Activities still to fetch.

    A manifest entry means "handled": either downloaded, or parked as unsupported
    because no parser handles its format yet. Parked ones stay out of the daily
    run — otherwise they'd be reported new on every single run and train the eye
    to skip the one line that matters. `--retry-unsupported` un-parks them once a
    new parser lands.
    """
    return [
        r for r in resources
        if r["id"] not in manifest
        or (retry_unsupported and "unsupported" in manifest[r["id"]])
    ]


def run(discover_only: bool, retry_unsupported: bool = False) -> str:
    """Archive whatever is new. Returns a summary, empty when nothing was new."""
    with single_run():
        return _run(discover_only, retry_unsupported)


def _run(discover_only: bool, retry_unsupported: bool = False) -> str:
    session = make_session()
    try:
        resources = discover(session)
    except SessionExpired:
        # Try one automatic re-login (if credentials are configured), else stop.
        if refresh_cookie():
            session = make_session()
            try:
                resources = discover(session)
            except SessionExpired:
                raise SessionExpired(
                    "el inicio automático falló — si cambiaste tu contraseña, ejecuta "
                    f"{_cmd()} --setup")
        else:
            raise SessionExpired(
                f"configura tus credenciales con {_cmd()} --setup, o actualiza "
                "aai.curlrc")
    manifest = load_manifest()
    new = pending(resources, manifest, retry_unsupported)
    parked = sum(1 for v in manifest.values() if "unsupported" in v)

    print(f"{len(resources)} actividades encontradas, {len(new)} nuevas"
          + (f", {parked} en espera (formato no soportado)." if parked else "."))
    for r in new:
        loc = str(dest_for(r).relative_to(ARCHIVE))
        print(f"  [nueva] ({r['kind']}) {r['name']}  ->  {loc}")

    if discover_only:
        return ""

    saved = []
    for r in new:
        dest = dest_for(r)
        print(f"Descargando: {r['name']} ...", flush=True)
        result = download(session, r, dest)
        if "skipped" in result:
            print(f"  en espera: {result['skipped']}")
            # Recorded, not downloaded: keeps it out of tomorrow's "new" list while
            # leaving a retryable breadcrumb for whoever writes the parser.
            manifest[r["id"]] = {
                "name": r["name"],
                "kind": r["kind"],
                "url": r["url"],
                "course": r["course_name"],
                "unsupported": result["skipped"],
                "parked_at": dt.datetime.now().isoformat(timespec="seconds"),
            }
            save_manifest(manifest)
            continue
        manifest[r["id"]] = {
            "name": r["name"],
            "kind": r["kind"],
            "url": r["url"],
            "course": r["course_name"],
            "dest": str(dest.relative_to(ARCHIVE)),
            "downloaded_at": dt.datetime.now().isoformat(timespec="seconds"),
            **result,
        }
        save_manifest(manifest)  # persist after each, so a crash loses at most one
        saved.append(manifest[r["id"]])
        if "chars" in result:
            note = f"  guardado: {result['chars']} caracteres, {len(result['media'])} multimedia"
            if result.get("oversize"):
                note += f" ({len(result['oversize'])} omitidos por tamaño)"
            print(note)
        else:
            print(f"  guardados {len(result['files'])} archivo(s)")

    sync_to_drive()
    resumen = summarize(saved)
    if saved:  # nothing downloaded means nothing to hand over; skip the CLI
        aviso = sync_to_notebooklm()
        if aviso:  # ride the summary out to Telegram, don't die in the log
            resumen += f"\n{aviso}"
    return resumen


def _tool_path(env_path: str, posix: bool = os.name != "nt") -> str:
    """Where to look for external tools like rclone.

    launchd (and cron) hand a job a minimal PATH — /usr/bin:/bin:/usr/sbin:/sbin —
    with no Homebrew in it, so `which rclone` fails under the daily agent even
    though it works in a terminal. Search the usual Homebrew prefixes too; on
    Linux they just don't exist, which shutil.which skips harmlessly.

    Windows has no Homebrew and separates PATH with ";", so its PATH is used
    as-is — Task Scheduler hands the job the full system PATH anyway.
    """
    if not posix:
        return env_path
    return ":".join(p for p in (env_path, "/opt/homebrew/bin", "/usr/local/bin") if p)


def _drive_remote(env: dict) -> str:
    """The rclone remote:path for the Drive backup; empty means disabled."""
    return env.get("DRIVE_REMOTE", "").strip()


def sync_to_drive() -> None:
    """Mirror the archive to Google Drive via rclone, if configured.

    ponytail: rclone already does Drive OAuth, token refresh and incremental
    upload — no reason to hand-roll the Drive API. `copy` (not `sync`) never
    deletes anything on Drive, matching this tool's append-only archive.
    """
    remote = _drive_remote(_load_env())
    if not remote:
        return
    import shutil
    import subprocess

    rclone = shutil.which("rclone", path=_tool_path(os.environ.get("PATH", "")))
    if not rclone:
        print("Respaldo a Drive omitido: rclone no está instalado (ver README).")
        return
    if not ARCHIVE.exists():
        return
    print(f"Respaldando en Drive ({remote}) ...", flush=True)
    rc = subprocess.run(
        [rclone, "copy", str(ARCHIVE), remote, "--fast-list"]
    ).returncode
    print("  Respaldo completado." if rc == 0 else f"  El respaldo falló (rclone salió con {rc}).")


def _notebooklm_cli(path_env: str, python: str) -> str:
    """The notebooklm CLI, or "" when it is not installed.

    Mirrors notebooklm_sync._cli_path, fallback included: pip puts the CLI next
    to the interpreter inside a venv or pyenv, and launchd runs the daily job
    with a minimal PATH where `which` alone would never find it. On Windows the
    venv's CLI is Scripts\\notebooklm.exe.
    """
    import shutil

    for name in ("notebooklm", "notebooklm.exe"):
        beside = pathlib.Path(python).parent / name
        if beside.exists():
            return str(beside)
    return shutil.which("notebooklm", path=_tool_path(path_env)) or ""


def _sync_outcome(rc: int | None) -> str:
    """What the Telegram summary should say about a finished sync.

    Empty when it went fine, so a good run stays quiet. `None` means it hit the
    timeout — the only outcome the user cannot see coming from the archive
    itself, so it says out loud that the material is downloaded but not synced.
    """
    if rc == 0:
        return ""
    if rc is None:
        return ("⚠ NotebookLM: la sincronización se colgó y se cortó. El material "
                "está descargado; se reintenta en la próxima corrida.")
    return f"⚠ NotebookLM: la sincronización falló (salió con {rc})."


def sync_to_notebooklm() -> str:
    """Hand the freshly archived material to NotebookLM, if its CLI is there.

    Returns the line for the Telegram summary; empty when there is nothing to
    report. Same contract as sync_to_drive: it never raises, because an optional
    tool must not take the archiving run down with it.

    ponytail: a subprocess instead of an import — notebooklm_sync calls
    sys.exit() when the CLI is missing, and that would end the run too.
    Installing the CLI is the opt-in; there is no second switch to forget.

    The timeout is the point, not a detail. The CLI talks to undocumented Google
    endpoints and bounds nothing itself, and this call sits inside the run lock:
    a stall here freezes the daily job AND the single-threaded bot, which is the
    exact outage resolve() already caused once.
    """
    import subprocess

    cli = _notebooklm_cli(os.environ.get("PATH", ""), sys.executable)
    if not cli:
        print("Sincronización con NotebookLM omitida: el CLI no está instalado (ver README).")
        return ""
    script = pathlib.Path(__file__).with_name("notebooklm_sync.py")
    print("Sincronizando con NotebookLM ...", flush=True)
    try:
        rc = subprocess.run([sys.executable, str(script)],
                            timeout=NOTEBOOKLM_TIMEOUT).returncode
    except subprocess.TimeoutExpired:
        # ponytail: kills the child, not any notebooklm CLI it spawned — those
        # are orphaned and exit on their own. Reaping the whole process group
        # is the upgrade if a stray CLI ever holds something open.
        rc = None
        print(f"  Se cortó por tiempo ({NOTEBOOKLM_TIMEOUT // 60} min).")
    else:
        print("  Sincronización completada." if rc == 0
              else f"  La sincronización falló (salió con {rc}).")
    return _sync_outcome(rc)


# --- Daily schedule ---------------------------------------------------------
# Installing the daily run means writing absolute paths into a launchd plist, a
# systemd unit or a schtasks command. That hand-editing is the step people get
# wrong, so `--install-schedule` renders it from the running interpreter and
# this file's own location. The renderers below are pure so they can be tested
# without touching the machine; install_schedule() is the thin shell that runs.

SCHEDULE_LABEL = "com.inacap.archiver"
SCHEDULE_HOUR = 8


def _plist(label: str, argv: list, log: str, when: str) -> str:
    """A launchd agent running argv, logging to log; `when` is its trigger."""
    e = html.escape  # a path may hold &, < or > — unescaped they break the XML
    args = "\n".join(f"    <string>{e(a)}</string>" for a in argv)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{e(label)}</string>
  <key>ProgramArguments</key>
  <array>
{args}
  </array>
{when}
  <key>StandardOutPath</key><string>{e(log)}</string>
  <key>StandardErrorPath</key><string>{e(log)}</string>
</dict>
</plist>
"""


def _schedule_plist(python: str, script: str) -> str:
    """A launchd agent that runs the archiver daily at SCHEDULE_HOUR."""
    return _plist(SCHEDULE_LABEL, [python, script], str(ROOT / "archiver.log"), f"""\
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key><integer>{SCHEDULE_HOUR}</integer>
    <key>Minute</key><integer>0</integer>
  </dict>""")


def _schedule_systemd(python: str, script: str) -> tuple[str, str]:
    """(service, timer) units for a daily user-level run."""
    log = str(ROOT / "archiver.log")
    service = f"""[Unit]
Description=Archivador INACAP

[Service]
Type=oneshot
ExecStart={python} {script}
StandardOutput=append:{log}
StandardError=append:{log}
"""
    timer = f"""[Unit]
Description=Archivador INACAP, todos los dias a las 0{SCHEDULE_HOUR}:00

[Timer]
OnCalendar=*-*-* 0{SCHEDULE_HOUR}:00:00
Persistent=true

[Install]
WantedBy=timers.target
"""
    return service, timer


def _schedule_schtasks(python: str, script: str) -> list[str]:
    """The schtasks argv that registers the daily task.

    /tr takes the whole command as one string, so both paths are quoted inside
    it; /f overwrites an existing task so re-running the installer is safe.
    """
    return [
        "schtasks", "/create", "/f",
        "/tn", SCHEDULE_LABEL,
        "/sc", "daily",
        "/st", f"0{SCHEDULE_HOUR}:00",
        "/tr", f'"{python}" "{script}"',
    ]


def install_schedule() -> None:
    """Register the daily run with whatever scheduler this OS provides."""
    import subprocess

    python = sys.executable
    script = str(pathlib.Path(__file__).resolve())

    if sys.platform == "darwin":
        target = pathlib.Path.home() / "Library/LaunchAgents" / f"{SCHEDULE_LABEL}.plist"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_schedule_plist(python, script), encoding="utf-8")
        # Unload first so re-running replaces the agent instead of erroring.
        subprocess.run(["launchctl", "unload", str(target)],
                       capture_output=True)
        rc = subprocess.run(["launchctl", "load", str(target)]).returncode
    elif sys.platform == "win32":
        rc = subprocess.run(_schedule_schtasks(python, script)).returncode
        target = SCHEDULE_LABEL
    else:
        unit_dir = pathlib.Path.home() / ".config/systemd/user"
        unit_dir.mkdir(parents=True, exist_ok=True)
        service, timer = _schedule_systemd(python, script)
        (unit_dir / "inacap-archiver.service").write_text(service, encoding="utf-8")
        (unit_dir / "inacap-archiver.timer").write_text(timer, encoding="utf-8")
        subprocess.run(["systemctl", "--user", "daemon-reload"])
        rc = subprocess.run(
            ["systemctl", "--user", "enable", "--now", "inacap-archiver.timer"]
        ).returncode
        target = str(unit_dir / "inacap-archiver.timer")

    if rc == 0:
        print(f"Programado todos los días a las 0{SCHEDULE_HOUR}:00 -> {target}")
    else:
        sys.exit(f"No se pudo programar la ejecución (salida {rc}). Ver README.")


# The Telegram bot as a background service, started at login. Same split as
# the daily run: pure renderers, and _bot_service() as the thin shell.
BOT_LABEL = "com.inacap.bot"
BOT_UNIT = "inacap-bot.service"


def _bot_plist(python: str, script: str) -> str:
    """The bot's launchd agent: starts at login, restarted if it dies."""
    return _plist(BOT_LABEL, [python, script, "--bot"], str(ROOT / "bot.log"), """\
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>60</integer>""")


def _bot_systemd(python: str, script: str) -> str:
    """The bot's systemd user service."""
    log = str(ROOT / "bot.log")
    return f"""[Unit]
Description=Bot de Telegram del archivador INACAP

[Service]
ExecStart="{python}" "{script}" --bot
Restart=on-failure
RestartSec=60
StandardOutput=append:{log}
StandardError=append:{log}

[Install]
WantedBy=default.target
"""


def _bot_task_xml(pythonw: str, script: str, user: str) -> str:
    """The bot's Task Scheduler definition, for `schtasks /create /xml`.

    Not plain `schtasks /sc onlogon`: that trigger is for any user, which is
    reported to need an elevated prompt, and its defaults stop the task after
    72 hours and whenever the laptop runs on battery. A logon trigger bound to
    this user, with those limits off, is what Register-ScheduledTask would make.
    pythonw.exe keeps a console window from staying open.
    """
    e = html.escape
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{e(user)}</UserId>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{e(user)}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{e(pythonw)}</Command>
      <Arguments>{e(f'"{script}" --bot')}</Arguments>
    </Exec>
  </Actions>
</Task>
"""


def _run_quiet(argv: list) -> int:
    """Run a scheduler command, output discarded; -1 when it is not installed."""
    import subprocess

    try:
        return subprocess.run(argv, capture_output=True).returncode
    except OSError:
        return -1


def _bot_service(start: bool) -> bool:
    """Stop and unregister the bot service; with start, register it and start it.

    Returns whether a bot service was registered before, so a setup that stops
    it can put it back if the student cancels.
    """
    call = _run_quiet
    home = pathlib.Path.home()
    commands, files = _service_teardown(sys.platform, home, bot_only=True)
    registered = (any(f.exists() for f in files) if files
                  else call(["schtasks", "/query", "/tn", BOT_LABEL]) == 0)
    for argv in commands:
        call(argv)
    for f in files:
        with contextlib.suppress(OSError):
            f.unlink()
    if not start:
        return registered

    python, script = sys.executable, str(pathlib.Path(__file__).resolve())
    if sys.platform == "darwin":
        target = home / "Library/LaunchAgents" / f"{BOT_LABEL}.plist"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_bot_plist(python, script), encoding="utf-8")
        rc = call(["launchctl", "load", str(target)])
    elif sys.platform == "win32":
        pythonw = str(pathlib.Path(python).with_name("pythonw.exe"))
        user = "\\".join(p for p in (os.environ.get("USERDOMAIN"),
                                     os.environ.get("USERNAME")) if p)
        xml = ROOT / "bot-task.xml"
        xml.write_text(_bot_task_xml(pythonw, script, user), encoding="utf-16")
        try:
            rc = (call(["schtasks", "/create", "/f", "/tn", BOT_LABEL, "/xml", str(xml)])
                  or call(["schtasks", "/run", "/tn", BOT_LABEL]))
        finally:
            xml.unlink()
    else:
        import shutil

        if not shutil.which("systemctl"):
            print(f"Este sistema no tiene systemd: inicia el bot a mano con: {_cmd()} --bot")
            return registered
        unit = home / ".config/systemd/user" / BOT_UNIT
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text(_bot_systemd(python, script), encoding="utf-8")
        call(["systemctl", "--user", "daemon-reload"])
        rc = call(["systemctl", "--user", "enable", "--now", BOT_UNIT])
    if rc == 0:
        print("✓ El bot atiende tus comandos (/bajar, /estado) mientras el equipo esté encendido.")
    else:
        print(f"✗ No se pudo dejar el bot en segundo plano (salida {rc}). "
              f"Puedes iniciarlo a mano con: {_cmd()} --bot")
    return registered


# --- Telegram ---------------------------------------------------------------
# Optional. Set TELEGRAM_TOKEN and TELEGRAM_CHAT_ID in .env (see README).
# Sending only needs an outbound POST — no server, no webhook, no open port.
_API = "https://api.telegram.org/bot{token}/{method}"
_MAX_MESSAGE = 4000  # Telegram's cap is 4096; leave room for the truncation mark


def _telegram_conf() -> tuple[str, str] | None:
    env = _load_env()
    token, chat = env.get("TELEGRAM_TOKEN"), env.get("TELEGRAM_CHAT_ID")
    return (token, chat) if token and chat else None


def summarize(saved: list[dict]) -> str:
    """One message listing what a run downloaded; empty when nothing was new."""
    if not saved:
        return ""
    lines = [f"INACAP: {len(saved)} recurso(s) nuevo(s)"]
    lines += [f"• [{r['course']}] {r['name']}" for r in saved]
    return "\n".join(lines)


def notify(text: str, chat_id: str | None = None) -> None:
    """Send a Telegram message. No-op when unconfigured; never fatal."""
    conf = _telegram_conf()
    if not conf or not text:
        return
    token, default_chat = conf
    if len(text) > _MAX_MESSAGE:
        text = text[:_MAX_MESSAGE] + "\n… (recortado)"
    try:
        _telegram_call(token, "sendMessage", {"chat_id": chat_id or default_chat, "text": text})
    except Exception as e:  # a failed notification must never sink a run
        print(f"No se pudo avisar por Telegram: {_redact(str(e))}")


def _chat_ids(payload: dict) -> list[tuple[str, str]]:
    """(chat_id, display name) for every chat seen in a getUpdates payload.

    An update wraps its chat under one of several keys (message, edited_message,
    channel_post...), so we take the chat off whichever one carries it. Ordered
    by first appearance and deduped, because the same person usually shows up in
    several updates.
    """
    out: dict[str, str] = {}
    for update in payload.get("result", []):
        for value in update.values():
            chat = value.get("chat") if isinstance(value, dict) else None
            if not chat or "id" not in chat:
                continue
            name = " ".join(
                p for p in (chat.get("first_name"), chat.get("last_name")) if p
            ) or chat.get("username") or chat.get("title") or "sin nombre"
            out.setdefault(str(chat["id"]), name)
    return list(out.items())


def _printable(text: str) -> str:
    """Text without control characters (escape sequences, bidi overrides)."""
    import unicodedata

    return "".join(c for c in text if unicodedata.category(c)[0] != "C")


def _private_chats(payload: dict) -> list[tuple[str, str, str]]:
    """(chat_id, name, last text) of each private chat that wrote, latest last.

    For --setup to recognise the student's own chat: groups and channels are
    never offered, and names and texts, which anyone can choose, are made safe
    to print.
    """
    out: dict[str, tuple[str, str]] = {}
    for update in payload.get("result", []):
        msg = update.get("message") or {}
        chat = msg.get("chat") or {}
        if chat.get("type") != "private" or "id" not in chat:
            continue
        name = " ".join(p for p in (chat.get("first_name"), chat.get("last_name")) if p) \
            or chat.get("username") or "sin nombre"
        out.pop(str(chat["id"]), None)  # re-insert: order by latest message
        out[str(chat["id"])] = (_printable(name), _printable(msg.get("text") or "")[:60])
    return [(cid, name, text) for cid, (name, text) in out.items()]


def _redact(text: str) -> str:
    """Hide bot tokens: request errors quote the URL, which carries the token."""
    return re.sub(r"\d{5,}:[A-Za-z0-9_-]{30,}", "<token>", text)


def _telegram_call(token: str, method: str, payload: dict | None = None) -> dict:
    """A Bot API call's JSON; {"ok": False, ...} when Telegram refuses it."""
    import requests

    return requests.post(_API.format(token=token, method=method),
                         json=payload or {}, timeout=30).json()


def telegram_setup() -> None:
    """Print the chat id to paste into .env, read from the bot's own updates."""
    token = _load_env().get("TELEGRAM_TOKEN")
    if not token:
        sys.exit("Falta TELEGRAM_TOKEN en el .env. Pídeselo a @BotFather.")
    payload = _telegram_call(token, "getUpdates")
    if not payload.get("ok"):
        sys.exit(f"Telegram rechazó la consulta: {payload.get('description')}")

    chats = _chat_ids(payload)
    if not chats:
        # A running --bot consumes updates and advances the offset, so this
        # command sees nothing while that service is up.
        sys.exit(
            "Ningún mensaje todavía. Escríbele algo a tu bot en Telegram y "
            "vuelve a ejecutar este comando.\n"
            "Si el bot ya está corriendo como servicio, deténlo primero: es él "
            "quien está recibiendo los mensajes."
        )
    print("Agrega esta línea a tu .env:\n")
    for chat_id, name in chats:
        print(f"  TELEGRAM_CHAT_ID={chat_id}       # {name}")
    if len(chats) > 1:
        print("\nHay más de una conversación; usa la tuya.")


# --- failure reporting -------------------------------------------------------
# An unattended job that dies quietly is worse than one that never ran: nobody
# reads a log that has always been fine. But notifying every hiccup teaches the
# reader to ignore the notifications, which ends in the same silence. So: report
# what the user must act on, and report persistent trouble even when each single
# failure looked recoverable.

LAST_RUN = ROOT / ".last-run.json"

# Network trouble by exception name, so requests need not be imported here.
# Builtin ConnectionError (an OSError) counts too.
_TRANSIENT = {"ConnectionError", "Timeout", "ConnectTimeout", "ReadTimeout",
              "ChunkedEncodingError", "SSLError", "ProxyError", "TimeoutError"}


def is_transient(exc: BaseException) -> bool:
    """True when tomorrow's run may well succeed without anyone doing anything."""
    return bool({c.__name__ for c in type(exc).__mro__} & _TRANSIENT)


def should_notify(exc: BaseException, consecutive: int) -> bool:
    """Whether this failure is worth a Telegram message.

    Anything the user must fix is reported at once. A transient failure waits:
    one dropped connection is noise, but two runs in a row means the archive has
    silently stopped growing, and that is worth knowing.
    """
    return not is_transient(exc) or consecutive >= 2


def previous_run() -> dict:
    """How the last run ended. Empty on a first run."""
    if not LAST_RUN.exists():
        return {}
    try:
        return json.loads(LAST_RUN.read_text(encoding="utf-8"))
    except ValueError:
        return {}  # truncated by a crash mid-write; treat as no history


def record_run(ok: bool, failures: int = 0, error: str = "",
               notified: bool = False) -> None:
    """Persist how this run ended. The caller owns the failure count, so that
    reading it and writing it can never drift apart."""
    LAST_RUN.write_text(json.dumps(
        {"ok": ok, "consecutive_failures": 0 if ok else failures,
         "error": error, "notified": notified,
         "at": dt.datetime.now().isoformat(timespec="seconds")},
        indent=2, ensure_ascii=False), encoding="utf-8")


# --- diagnóstico (--check) ---------------------------------------------------
# Cuando algo no anda, un error suelto no dice en qué paso se rompió. Esto
# recorre la instalación entera en orden de dependencia y deja ver de un vistazo
# qué falta. Lo opcional que no está configurado NO es un fallo: es una elección.

OK, WARN, FAIL, SKIP = "ok", "warn", "fail", "skip"
_MARK = {OK: "✓", WARN: "!", FAIL: "✗", SKIP: "·"}
MIN_PYTHON = (3, 9)


def _mask(value: str) -> str:
    """Deja los extremos a la vista y tapa el medio.

    Alcanza para reconocer un valor propio sin publicarlo: quien pide ayuda
    pegando esta salida no debería estar entregando su RUT ni su chat id.
    """
    if len(value) <= 4:
        return "·" * len(value)
    return f"{value[:2]}{'·' * (len(value) - 5)}{value[-3:]}"


def check_python(info=None) -> tuple:
    v = tuple(info or sys.version_info[:3])
    shown = ".".join(str(n) for n in v)
    if v >= MIN_PYTHON:
        return OK, f"Python {shown}"
    return FAIL, f"Python {shown} — hace falta {'.'.join(map(str, MIN_PYTHON))} o superior"


def exit_code(results) -> int:
    """1 solo si algo está roto. Lo no configurado y los avisos no cuentan."""
    return 1 if any(status == FAIL for status, _ in results) else 0


def _check_deps() -> tuple:
    faltan = []
    for mod in ("requests", "playwright"):
        try:
            __import__(mod)
        except ImportError:
            faltan.append(mod)
    if faltan:
        return FAIL, (f"falta {', '.join(faltan)} — "
                      "python3 -m pip install -r requirements.txt")
    return OK, "requests y playwright instalados"


def _check_credentials() -> tuple:
    env = _load_env()
    if not (ROOT / ".env").exists():
        return FAIL, f"no existe .env — ejecuta {_cmd()} --setup"
    if env.get("INACAP_USER") and env.get("INACAP_PASS"):
        return OK, f"INACAP_USER={_mask(env['INACAP_USER'])}"
    if (ROOT / "aai.curlrc").exists():
        return WARN, "sin credenciales; se usa la cookie manual de aai.curlrc"
    return FAIL, f"faltan INACAP_USER e INACAP_PASS — ejecuta {_cmd()} --setup"


def _check_moodle() -> tuple:
    try:
        cursos = discover_courses(make_session())
        return OK, f"{len(cursos)} curso(s) accesibles"
    except SystemExit as e:
        return FAIL, str(e).splitlines()[0]
    except SessionExpired:
        return (OK, "la sesión estaba vencida y se renovó sola") if refresh_cookie() \
            else (FAIL, f"no se pudo iniciar sesión — corrígelas con {_cmd()} --setup")
    except Exception as e:
        return FAIL, f"{type(e).__name__}: {e}"


def _check_drive() -> tuple:
    import shutil

    remote = _drive_remote(_load_env())
    if not remote:
        return SKIP, "desactivado (DRIVE_REMOTE vacío en el .env)"
    if not shutil.which("rclone", path=_tool_path(os.environ.get("PATH", ""))):
        return FAIL, f"DRIVE_REMOTE={remote} pero rclone no está instalado"
    return OK, f"respaldo en {remote}"


def _check_telegram() -> tuple:
    conf = _telegram_conf()
    if not conf:
        return SKIP, "desactivado (sin TELEGRAM_TOKEN o TELEGRAM_CHAT_ID)"
    import requests

    try:
        r = requests.get(_API.format(token=conf[0], method="getMe"), timeout=20)
        data = r.json()
        if not data.get("ok"):
            return FAIL, f"Telegram rechazó el token: {data.get('description')}"
        return OK, f"bot @{data['result'].get('username')}, chat {_mask(conf[1])}"
    except Exception as e:
        return WARN, f"no se pudo comprobar ahora: {type(e).__name__}"


def _check_schedule() -> tuple:
    import subprocess

    if sys.platform == "darwin":
        target = pathlib.Path.home() / "Library/LaunchAgents" / f"{SCHEDULE_LABEL}.plist"
        listo = target.exists()
    elif sys.platform == "win32":
        listo = subprocess.run(["schtasks", "/query", "/tn", SCHEDULE_LABEL],
                               capture_output=True).returncode == 0
    else:
        listo = (pathlib.Path.home()
                 / ".config/systemd/user/inacap-archiver.timer").exists()
    if listo:
        return OK, f"programada a las 0{SCHEDULE_HOUR}:00"
    return WARN, f"sin programar — usa: {_cmd()} --install-schedule"


def check() -> None:
    """Recorre la instalación y muestra qué está listo y qué falta."""
    ultima = previous_run()
    pasos = [
        ("Python", check_python()),
        ("Dependencias", _check_deps()),
        ("Credenciales", _check_credentials()),
        ("Sesión de Moodle", _check_moodle()),
        ("Google Drive", _check_drive()),
        ("Telegram", _check_telegram()),
        ("Ejecución diaria", _check_schedule()),
    ]
    ancho = max(len(n) for n, _ in pasos)
    for nombre, (status, detalle) in pasos:
        print(f"  {_MARK[status]}  {nombre.ljust(ancho)}  {detalle}")

    if ultima:
        estado = "correcta" if ultima.get("ok") else \
            f"fallida ({ultima.get('consecutive_failures', 0)} seguidas): {ultima.get('error', '')}"
        print(f"\n  Última ejecución: {estado} — {ultima.get('at', '')}")

    resultados = [r for _, r in pasos]
    if exit_code(resultados):
        sys.exit("\nHay algo roto (✗). Revisa las líneas marcadas.")
    print("\nTodo en orden.")


def report_failure(exc: BaseException) -> bool:
    """Record a failed run, and speak up when it is worth speaking up.

    Returns whether a notification was sent, which the next successful run reads
    back to decide if it owes the user an all-clear.
    """
    fails = previous_run().get("consecutive_failures", 0) + 1
    aviso = should_notify(exc, fails)
    record_run(False, fails, f"{type(exc).__name__}: {exc}", aviso)
    if aviso:
        veces = f" ({fails} ejecuciones seguidas)" if fails > 1 else ""
        notify(f"INACAP: la ejecución diaria falló{veces}.\n{exc}")
    return aviso


def report_success(summary: str) -> None:
    """Close the loop: if the user was told about a failure, tell them it ended."""
    if previous_run().get("notified"):
        notify("INACAP: la ejecución diaria volvió a funcionar.")
    record_run(True)
    notify(summary)  # silent when nothing is new


def parse_command(update: dict) -> tuple[str, str]:
    """(command, chat_id) from a Telegram update; ("", "") if it isn't one."""
    msg = update.get("message") or {}
    chat = str((msg.get("chat") or {}).get("id", ""))
    parts = (msg.get("text") or "").strip().split()
    if not parts or not parts[0].startswith("/"):
        return "", chat
    return parts[0].split("@")[0].lower(), chat  # "/bajar@mibot" -> "/bajar"


def handle(cmd: str) -> str:
    if cmd == "/bajar":
        try:
            return run(discover_only=False) or "Sin novedades."
        except Busy:
            return "Ya hay una corrida en curso, esperá a que termine."
        except SessionExpired as e:
            return f"Sesión de Moodle caída: {e}"
        except Exception as e:
            return f"La corrida falló: {e}"
    if cmd == "/estado":
        manifest = load_manifest()
        last = max((r.get("downloaded_at", "") for r in manifest.values()),
                   default="") or "nunca"
        return f"{len(manifest)} recursos archivados.\nÚltima descarga: {last}"
    return "Comandos: /bajar  /estado"


def bot() -> None:
    """Serve commands over Telegram long-polling.

    ponytail: getUpdates means no webhook, no public IP and no HTTP server —
    the Mac only makes outbound calls. Single-threaded on purpose: /bajar takes
    minutes and blocks the poll, which is fine for one user (the lock keeps the
    daily cron honest anyway). Move to Cloud Run Jobs if that ever bites.
    """
    conf = _telegram_conf()
    if not conf:
        sys.exit("Configura TELEGRAM_TOKEN y TELEGRAM_CHAT_ID en el .env (ver README).")
    import requests

    token, owner = conf
    offset = 0
    print("Bot escuchando (ctrl-C para detener).", flush=True)
    notify("Bot INACAP en línea. Comandos: /bajar  /estado")
    while True:
        try:
            r = requests.get(
                _API.format(token=token, method="getUpdates"),
                params={"offset": offset, "timeout": 50},
                timeout=70,
            )
            payload = r.json()
            if not payload.get("ok"):
                # A revoked or mistyped token fails every poll; retrying is
                # pointless and silent. Die loudly so the log says why.
                sys.exit(f"Telegram rechazó la consulta: {payload.get('description')}")
            updates = payload["result"]
        except Exception as e:
            print(f"consulta fallida: {_redact(str(e))}", flush=True)
            time.sleep(15)  # network hiccup or Telegram blip; back off and retry
            continue
        for update in updates:
            offset = update["update_id"] + 1
            cmd, chat = parse_command(update)
            # Anyone can message a bot: only the owner's chat is ever served.
            if not cmd or chat != owner:
                continue
            print(f"comando: {cmd}", flush=True)
            if cmd == "/bajar":
                notify("Buscando novedades…", chat)
            notify(handle(cmd), chat)


# --- Uninstall --------------------------------------------------------------
# What to remove is decided by pure functions (tested in _extras_test);
# uninstall() only carries it out.

# The repo's top-level entries, i.e. what the installer unpacks into ROOT.
# _gitignore_test checks it against `git ls-files`.
_REPO_FILES = (".env.example", ".github", ".gitignore", "LICENSE", "README.md",
               "archiver.py", "install.ps1", "install.sh", "notebooklm_sync.py",
               "requirements.txt")

# What notebooklm-py keeps in its home: the per-profile folder, its config, and
# the pre-profile layout (storage_state.json, its master_token.json sibling,
# context.json, browser_profile/). Its MCP server's oauth/ is not ours to touch.
_NOTEBOOKLM_ENTRIES = ("profiles", "config.json", "storage_state.json",
                       "context.json", "master_token.json", "browser_profile")

# Personal data the archiver leaves in ROOT, all of it gitignored.
_PERSONAL = (".env", "*.curlrc", "cookies*.txt", "notebooklm.json",
             "manifest.json", ".last-run.json", "*.log", ".lock")


def _launcher(home: pathlib.Path, windows: bool, xdg_bin: str) -> pathlib.Path:
    """The `inacap` command the installers write, by the same rule they use."""
    return pathlib.Path(xdg_bin or home / ".local/bin") / ("inacap.cmd" if windows else "inacap")


def _service_teardown(platform: str, home: pathlib.Path,
                      bot_only: bool = False) -> tuple[list, list]:
    """(commands, files) that stop and remove the bot, and the daily run too
    unless bot_only, on this OS."""
    labels = (BOT_LABEL,) if bot_only else (BOT_LABEL, SCHEDULE_LABEL)
    if platform == "darwin":
        files = [home / "Library/LaunchAgents" / f"{label}.plist" for label in labels]
        return [["launchctl", "unload", str(f)] for f in files], files
    if platform == "win32":
        return ([["schtasks", "/end", "/tn", BOT_LABEL]]
                + [["schtasks", "/delete", "/f", "/tn", label] for label in labels]), []
    units = (BOT_UNIT,) if bot_only else ("inacap-archiver.timer", BOT_UNIT)
    unit_dir = home / ".config/systemd/user"
    extra = () if bot_only else ("inacap-archiver.service",)
    return ([["systemctl", "--user", "disable", "--now", *units]],
            [unit_dir / name for name in (*extra, *units)])


def _installer_dir(root: pathlib.Path, home: pathlib.Path, names) -> bool:
    """Whether root is a folder the installer made, and so safe to delete whole.

    A git checkout never is (it is somebody's work), and neither is the home
    folder or anything containing it, whatever happens to be inside.
    """
    return (".git" not in names
            and {"archiver.py", "requirements.txt", ".venv"} <= set(names)
            and root != home and root not in home.parents)


def _uninstall_plan(names, wipe: bool, delete_archive: bool,
                    windows: bool) -> tuple[list, list, list]:
    """(delete now, delete after exit, what stays) for the entries in ROOT.

    Only what this tool put there goes: personal data, and with `wipe` the
    program too (_REPO_FILES, .venv, __pycache__). INACAP_DIR may be a folder
    that already held the student's own files; those stay, and so does ROOT.
    `later` is what this very process keeps locked on Windows: .venv holds the
    running python.exe.
    """
    import fnmatch

    ours = {n for n in names if any(fnmatch.fnmatchcase(n, p) for p in _PERSONAL)}
    if wipe:
        ours |= set(_REPO_FILES) | {".venv", "__pycache__"}
    if delete_archive:
        ours.add("archive")
    later = [".venv"] if wipe and windows and ".venv" in names else []
    return (sorted(n for n in names if n in ours and n not in later), later,
            sorted(n for n in names if n not in ours))


def _deferred_removal(trees: list, files: list, empty_dir: str) -> str:
    """A cmd line that deletes these once this process has exited (Windows).

    The wait is a ping: `timeout` aborts at once when there is no console.
    empty_dir gets a plain rmdir, which refuses a folder that is not empty.
    """
    steps = (["ping -n 3 127.0.0.1 >nul"] + [f'rmdir /s /q "{d}"' for d in trees]
             + [f'del /f /q "{f}"' for f in files]
             + ([f'rmdir "{empty_dir}"'] if empty_dir else []))
    return f'cmd /c "{" & ".join(steps)}"'


def _notebooklm_targets(nlm: pathlib.Path, home: pathlib.Path) -> list:
    """notebooklm-py's own entries in its home (its paths.py), and nothing else.

    NOTEBOOKLM_HOME can point anywhere, so it is never deleted wholesale, and
    nothing at all when it resolves to the home folder or above it.
    """
    nlm, home = nlm.resolve(), home.resolve()
    if nlm == home or nlm in home.parents:
        return []
    return [nlm / name for name in _NOTEBOOKLM_ENTRIES]


def _leftovers(platform: str, home: pathlib.Path) -> str:
    """What --uninstall cannot remove, and how to do it by hand."""
    cache = {"darwin": home / "Library/Caches/ms-playwright",
             "win32": home / "AppData/Local/ms-playwright"}.get(
                 platform, home / ".cache/ms-playwright")
    uv = ('uv cache clean; Remove-Item -Recurse "$(uv python dir)", '
          r'~\.local\bin\uv.exe, ~\.local\bin\uvx.exe' if platform == "win32" else
          'uv cache clean && rm -rf "$(uv python dir)" ~/.local/bin/uv ~/.local/bin/uvx')
    return ("Esto no lo borré yo:\n"
            "  • La copia en tu Google Drive (carpeta INACAP), si activaste el respaldo.\n"
            "    Bórrala desde drive.google.com si ya no la quieres.\n"
            "  • Tu bot de Telegram, si creaste uno: envíale /deletebot a @BotFather.\n"
            "  • El navegador del inicio de sesión (Playwright), que otros programas\n"
            f"    pueden compartir: {cache}\n"
            "  • uv, el gestor de Python. Si nada más lo usa:\n"
            f"      {uv}")


def uninstall(root: pathlib.Path = ROOT, home: pathlib.Path | None = None,
              run=_run_quiet, spawn=None) -> None:
    """Remove schedules, bot and personal data; the program too if the installer made it.

    root, home and run (the scheduler command runner) are parameters so the
    self-test can uninstall a temp install without touching this machine.
    """
    import shutil
    import subprocess

    home = home or pathlib.Path.home()
    archive = root / "archive"
    windows = sys.platform == "win32"
    names = [p.name for p in root.iterdir()]
    wipe = _installer_dir(root.resolve(), home.resolve(), names)
    print("Esto quita la ejecución diaria y el bot de Telegram, y borra tus credenciales,\n"
          "sesiones y registros" + (f", junto con el programa ({root})." if wipe else "."))
    if input('Escribe "si" para continuar: ').strip().lower() not in ("si", "sí"):
        print("No se borró nada.")
        return
    delete_archive = archive.exists() and _yes(
        f"¿Borrar también tu material descargado ({archive})? [s/N] ", default=False)

    commands, files = _service_teardown(sys.platform, home)
    for argv in commands:
        run(argv)
    now, later, left = _uninstall_plan(names, wipe, delete_archive, windows)
    nlm = _notebooklm_targets(_notebooklm_home(home), home)
    targets = files + [root / n for n in now] + nlm
    launcher = _launcher(home, windows, os.environ.get("XDG_BIN_HOME", ""))
    if wipe and not windows:  # on Windows it is the .cmd running us: deferred
        targets.append(launcher)
    failed = []
    for p in targets:
        try:
            if p.is_dir() and not p.is_symlink():
                shutil.rmtree(p)
            elif p.exists() or p.is_symlink():
                p.unlink()
        except OSError:
            failed.append(p)
    if sys.platform not in ("darwin", "win32"):
        run(["systemctl", "--user", "daemon-reload"])
    if nlm:
        with contextlib.suppress(OSError):  # still holds other things: it stays
            nlm[0].parent.rmdir()
    if wipe and not left and not windows:
        try:
            root.rmdir()
        except OSError:
            failed.append(root)
    if windows and wipe:
        cmd = _deferred_removal([str(root / n) for n in later], [str(launcher)],
                                "" if left else str(root))
        (spawn or subprocess.Popen)(cmd, cwd=str(home), creationflags=subprocess.DETACHED_PROCESS
                                    | subprocess.CREATE_NEW_PROCESS_GROUP)

    print("\n✓ Se quitaron la ejecución diaria y el bot, y se borraron tus datos personales.")
    if wipe and windows:
        # The deferred removal runs after we exit, so its result is unknown here.
        pending = root if not left else root / ".venv"
        print(f"El programa ({root}) termina de borrarse en unos segundos. Si después "
              f"{pending} sigue ahí, bórrala a mano.")
    elif wipe:
        print(f"✓ Se borró el programa ({root}).")
    elif (root / ".git").exists():
        print(f"Esta carpeta es un clon de git ({root}): el código no se tocó.")
    else:
        print(f"Esta carpeta no la creó el instalador: el programa sigue en {root}.")
    if archive.exists() and not delete_archive:
        print(f"Tu material sigue en: {archive}")
    ajenos = [n for n in left if n != "archive"]
    if wipe and ajenos:
        print(f"No toqué lo que no es del programa, que sigue en {root}: {', '.join(ajenos)}")
    for p in failed:
        print(f"✗ No se pudo borrar: {p}")
    print("\n" + _leftovers(sys.platform, home))


def self_test() -> None:
    sample = '''
      <title>Curso: Minería de Datos | AAI</title>
      <a href="https://aai.inacap.cl/mod/url/view.php?id=1946309">
        <span class="instancename">Recurso digital: Datos en todas partes.
          <span class="accesshide"> URL</span></span></a>
      <a href="/mod/folder/view.php?id=1946269"><span class="instancename">Material complementario U1
        <span class="accesshide"> Carpeta</span></span></a>
      <a href="/mod/assign/view.php?id=2128923"><span class="instancename">Taller 1
        <span class="accesshide"> Tarea</span></span></a>
      <a href="/mod/quiz/view.php?id=999"><span class="instancename">Autoevaluación
        <span class="accesshide"> Cuestionario</span></span></a>
    '''
    res = parse_resources(sample)
    assert ("1946309", "url", "Recurso digital: Datos en todas partes.") in res, res
    assert ("1946269", "folder", "Material complementario U1") in res, res
    assert ("2128923", "assign", "Taller 1") in res, res
    assert not any(r[0] == "999" for r in res), "quiz is not archivable"
    assert course_name(sample, 62864) == "Minería de Datos", course_name(sample, 62864)
    assert _safe_name("A/B: C?") == "A-B- C-", _safe_name("A/B: C?")

    parts = repo_parts(
        "https://virtual.inacap.cl/repositorio/TI3061_ASP/U1/TI3061_U1_S1_RD/content/index.php?sci=x"
    )
    assert parts == ("TI3061_ASP", "U1", "TI3061_U1_S1_RD"), parts
    assert repo_parts("https://aai.inacap.cl/my/") is None

    course = {
        "title": "RD Demo",
        "description": "<p>Intro &amp; welcome</p>",
        "lessons": [
            {"title": "Lectura", "items": [{"text": "<p>Cuerpo de la lección.</p>"}]}
        ],
    }
    md = _render_markdown(course)
    assert "# RD Demo" in md and "## Lectura" in md, md
    assert "Intro & welcome" in md and "Cuerpo de la lección." in md, md

    # A link block is a citation: its URL has to reach the markdown, or
    # notebooklm_sync finds nothing to mine and the reader is told to "go read"
    # something unreachable. Artwork keys stay out. The escaped "&" matters —
    # the library permalinks carry query strings.
    cited = {
        "title": "Bibliografía",
        "lessons": [{"title": "Unidad 1", "items": [{
            "type": "link",
            "label": "IR a la lectura",
            "destination": "https://biblioteca.example/permalink?a=1&amp;b=2",
            "thumbnail": "https://cdn.example/portada.png",
        }]}],
    }
    md = _render_markdown(cited)
    assert "https://biblioteca.example/permalink?a=1&b=2" in md, md
    assert "portada.png" not in md, md

    # originalUrl is only sometimes a URL: the same field carries asset
    # filenames and the authors' image-generation prompts, so a bare match
    # would bury the material under noise. Only the player address survives,
    # unwrapped from the <iframe> when that is how it was pasted.
    assert _embed_url({"originalUrl": "https://vimeo.com/123"}) == "https://vimeo.com/123"
    assert _embed_url({"originalUrl": '<iframe src="https://v.cl/p.html?a=1&amp;b=2" '
                                      'width="640"></iframe>'}) == "https://v.cl/p.html?a=1&b=2"
    assert _embed_url({"originalUrl": "mountains.jpg"}) is None
    assert _embed_url({"originalUrl": "icono-de-lupa.-No-incluir-palabras."}) is None
    assert _embed_url({"crushedKey": "x.png"}) is None

    con_video = {
        "title": "Con video",
        "lessons": [{"title": "U1", "items": [
            {"type": "embed", "originalUrl": "https://player.vimeo.com/video/99"},
            {"type": "image", "originalUrl": "una-oficina-moderna.-No-incluir-texto."},
        ]}],
    }
    md = _render_markdown(con_video)
    assert "https://player.vimeo.com/video/99" in md, md
    assert "oficina-moderna" not in md, md

    data = {
        "course": course,
        "x": {"crushedKey": "foto.png", "useCrushedKey": True, "type": "image"},
        "y": {"type": "attachment", "url": "path/to/Lectura.pdf"},
    }
    assert _collect_assets(data) == {"foto.png", "Lectura.pdf"}, _collect_assets(data)

    folder_html = '''
      <a href="https://aai.inacap.cl/pluginfile.php/1/core_admin/favicon/64x64/1/favicon.ico">x</a>
      <a href="https://aai.inacap.cl/pluginfile.php/9/mod_folder/content/0/Apunte.pptx?forcedownload=1">x</a>
      <img src="https://aai.inacap.cl/pluginfile.php/9/mod_folder/content/0/Apunte.pptx?preview=tinyicon&amp;oid=1">
    '''
    files = _content_pluginfiles(folder_html)
    assert files == [
        "https://aai.inacap.cl/pluginfile.php/9/mod_folder/content/0/Apunte.pptx?forcedownload=1"
    ], files  # core_admin dropped, preview thumbnail deduped away

    # An assign page carries the teacher's brief (mod_assign/introattachment) and,
    # once handed in, the student's own upload (assignsubmission_file). Keep both.
    assign_html = '''
      <a href="https://aai.inacap.cl/pluginfile.php/1/core_admin/logocompact/300x300/1/logo.png">x</a>
      <a href="https://aai.inacap.cl/pluginfile.php/9/mod_assign/introattachment/0/Taller%201.docx?forcedownload=1">x</a>
      <a href="https://aai.inacap.cl/pluginfile.php/9/assignsubmission_file/submission_files/7/Entrega.docx?forcedownload=1">x</a>
    '''
    assert _content_pluginfiles(assign_html) == [
        "https://aai.inacap.cl/pluginfile.php/9/assignsubmission_file/submission_files/7/Entrega.docx?forcedownload=1",
        "https://aai.inacap.cl/pluginfile.php/9/mod_assign/introattachment/0/Taller%201.docx?forcedownload=1",
    ], _content_pluginfiles(assign_html)

    assert _fix_mojibake("Caso PrÃ¡ctico") == "Caso Práctico", _fix_mojibake("Caso PrÃ¡ctico")
    assert _fix_mojibake("Big Data.pdf") == "Big Data.pdf"  # untouched when clean

    # A missing or unusable cookie file must not kill the process: on a fresh
    # install the auto-login is what creates it, and it gets first refusal.
    # .gitignore has no `cookie = "..."` line; the README does, in an example.
    for bad in ("no-such-file.curlrc", ".gitignore"):  # absent, then no cookie line
        try:
            load_cookie(bad)
        except SessionExpired:
            pass
        else:
            raise AssertionError(f"load_cookie({bad!r}) must raise, not exit")

    # A parked (unsupported) activity must stop counting as new, or the daily log
    # cries wolf forever — but --retry-unsupported must still pick it back up.
    res = [{"id": "1"}, {"id": "2"}, {"id": "3"}]
    man = {"2": {"downloaded_at": "2026-08-31T08:00:00"}, "3": {"unsupported": "not Rise"}}
    assert [r["id"] for r in pending(res, man)] == ["1"], pending(res, man)
    assert [r["id"] for r in pending(res, man, retry_unsupported=True)] == ["1", "3"]
    assert pending(res, {}) == res, "an empty manifest leaves everything pending"

    # Drive backup is opt-in and configured in .env, so a fresh clone never
    # tries to sync to somebody else's remote. .env values arrive unstripped.
    assert _drive_remote({}) == "", "Drive must be off unless configured"
    assert _drive_remote({"DRIVE_REMOTE": ""}) == ""
    assert _drive_remote({"DRIVE_REMOTE": " gdrive:INACAP "}) == "gdrive:INACAP"

    # NotebookLM is handed the new material as a subprocess, so it has to be
    # located first. launchd gives the daily run a minimal PATH, so `which`
    # alone misses a CLI pip put beside the interpreter — which is exactly where
    # it lives in a venv or pyenv. Missing CLI means skip, never crash.
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        binario = pathlib.Path(tmp) / "bin"
        binario.mkdir()
        (binario / "notebooklm").write_text("")
        assert _notebooklm_cli("", str(binario / "python3")) == str(binario / "notebooklm"), \
            "the CLI beside the interpreter is what launchd can actually find"
        assert _notebooklm_cli("", str(pathlib.Path(tmp) / "vacio" / "python3")) == "", \
            "no CLI means the sync is skipped, not a failed run"
        # A Windows venv has Scripts\notebooklm.exe: the bare name never exists
        # there, and Task Scheduler does not put Scripts on PATH either.
        scripts = pathlib.Path(tmp) / "Scripts"
        scripts.mkdir()
        (scripts / "notebooklm.exe").write_text("")
        assert _notebooklm_cli("", str(scripts / "python.exe")) == str(scripts / "notebooklm.exe")

    # A sync that went fine must not add noise to the Telegram summary, and one
    # that did not must never die inside the log: this is the line that carries
    # it out. None is the timeout, which is the case that used to hang forever.
    assert _sync_outcome(0) == "", "a clean sync stays out of the summary"
    assert "colgó" in _sync_outcome(None), _sync_outcome(None)
    assert "3" in _sync_outcome(3), "a failing exit code has to be readable"

    # The scheduler recipes are rendered from absolute paths the caller resolves.
    # Getting those paths right by hand is the step that trips people up, so the
    # rendering is what gets tested; installing them is a thin shell around it.
    plist = _schedule_plist("/usr/bin/python3", "/home/a b/archiver.py")
    assert "<string>/usr/bin/python3</string>" in plist, plist
    assert "<string>/home/a b/archiver.py</string>" in plist, plist
    assert "<key>Hour</key><integer>8</integer>" in plist, plist
    # A path may contain XML metacharacters; unescaped they corrupt the plist.
    assert "R&amp;D" in _schedule_plist("/p", "/R&D/archiver.py")

    service, timer = _schedule_systemd("/usr/bin/python3", "/opt/a/archiver.py")
    assert "ExecStart=/usr/bin/python3 /opt/a/archiver.py" in service, service
    assert "OnCalendar=*-*-* 08:00:00" in timer, timer
    assert "Persistent=true" in timer, timer  # catches up after the PC was off

    argv = _schedule_schtasks("C:\\Py\\pythonw.exe", "C:\\a b\\archiver.py")
    assert "/f" in argv, argv  # re-running the installer must not fail
    assert '"C:\\Py\\pythonw.exe" "C:\\a b\\archiver.py"' in argv, argv

    assert summarize([]) == "", "nothing new must produce no message"
    msg = summarize([
        {"course": "Minería de Datos", "name": "Recurso digital: Clustering"},
        {"course": "Big Data", "name": "Clase 3"},
    ])
    assert msg.startswith("INACAP: 2 recurso(s) nuevo(s)"), msg
    assert "• [Big Data] Clase 3" in msg, msg

    # --telegram-setup reads the chat id off getUpdates so nobody has to open the
    # raw API in a browser and hunt for a number in the JSON.
    payload = {"result": [
        {"message": {"chat": {"id": 111, "first_name": "Ana", "last_name": "Soto"}}},
        {"message": {"chat": {"id": 111, "first_name": "Ana"}}},          # deduped
        {"edited_message": {"chat": {"id": 222, "username": "pedro"}}},   # also counts
        {"my_chat_member": {}},                                           # no chat, ignored
    ]}
    assert _chat_ids(payload) == [("111", "Ana Soto"), ("222", "pedro")], _chat_ids(payload)
    assert _chat_ids({"result": []}) == []

    # --check: lo opcional sin configurar no es un fallo, es una elección. Solo
    # lo roto hace salir con error, para que el diagnóstico sirva de verdad.
    # La salida de --check está pensada para pegarla en un issue pidiendo ayuda,
    # así que no puede llevar el RUT ni el chat id en claro.
    assert _mask("22066784-7") == "22·····4-7", _mask("22066784-7")
    assert _mask("5623909655") == "56·····655"
    assert len(_mask("22066784-7")) == len("22066784-7"), "conserva el largo"
    assert _mask("ab") == "··", "demasiado corto para mostrar algo"
    assert _mask("") == ""

    assert check_python((3, 11, 9))[0] == OK
    assert check_python((3, 9, 6))[0] == OK, "3.9 es el mínimo soportado"
    assert check_python((3, 8, 10))[0] == FAIL
    assert exit_code([(OK, "a"), (SKIP, "b")]) == 0, "lo opcional no rompe"
    assert exit_code([(OK, "a"), (WARN, "b")]) == 0, "un aviso tampoco"
    assert exit_code([(OK, "a"), (FAIL, "b")]) == 1
    assert exit_code([]) == 0

    # Qué merece interrumpir a alguien. Un corte de red se arregla solo mañana;
    # una credencial rechazada, no. Lo desconocido se avisa: es más seguro
    # molestar por algo nuevo que tragárselo.
    class ReadTimeout(Exception): pass          # imita a requests.exceptions
    class SSLError(Exception): pass
    assert is_transient(ReadTimeout()), "un timeout de red es transitorio"
    assert is_transient(SSLError())
    assert is_transient(ConnectionError())      # el builtin, también de red
    assert not is_transient(SessionExpired("cookie vencida"))
    assert not is_transient(ValueError("algo nuevo")), "lo desconocido se avisa"

    # Transitorio: se calla la primera vez, avisa si insiste.
    assert not should_notify(ReadTimeout(), 1)
    assert should_notify(ReadTimeout(), 2), "dos días seguidos sin bajar nada sí"
    # Accionable: avisa de entrada.
    assert should_notify(SessionExpired("x"), 1)

    upd = {"update_id": 7, "message": {"chat": {"id": 12345}, "text": "/Bajar@mibot ya"}}
    assert parse_command(upd) == ("/bajar", "12345"), parse_command(upd)
    assert parse_command({"message": {"chat": {"id": 1}, "text": "hola"}}) == ("", "1")
    assert parse_command({"edited_message": {}}) == ("", "")  # nothing to serve

    # On macOS, launchd's minimal PATH must still find Homebrew tools. On Linux
    # the extra prefixes simply don't exist, which shutil.which tolerates.
    assert _tool_path("/usr/bin:/bin", posix=True) == \
        "/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin"
    assert _tool_path("", posix=True).startswith("/opt/homebrew/bin")
    # Windows separates PATH with ";" and has no Homebrew — leave it untouched.
    assert _tool_path(r"C:\Windows;C:\rclone", posix=False) == r"C:\Windows;C:\rclone"

    # --setup escribe las credenciales en el .env sin tocar nada más: ahí
    # también viven Drive y Telegram, que alguien configuró a mano.
    plantilla = ("# Credenciales\nINACAP_USER=tu-correo@inacapmail.cl\n"
                 "INACAP_PASS=tu-contraseña\n# INACAP_USER=comentado\nDRIVE_REMOTE=\n")
    def creds(user, pw):
        return {"INACAP_USER": user, "INACAP_PASS": pw}

    nuevo = _with_env(None, plantilla, creds("ana@inacapmail.cl", "secreta"))
    assert _parse_env(nuevo) == {"INACAP_USER": "ana@inacapmail.cl",
                                 "INACAP_PASS": "secreta", "DRIVE_REMOTE": ""}, nuevo
    assert "# Credenciales" in nuevo and "# INACAP_USER=comentado" in nuevo, nuevo
    assert "tu-contraseña" not in nuevo, "el placeholder se reemplaza"
    actual = "TELEGRAM_TOKEN=123:abc\nINACAP_USER=vieja\nINACAP_PASS=vieja\n"
    env = _parse_env(_with_env(actual, plantilla, creds("ana", "nueva")))
    assert env == {"TELEGRAM_TOKEN": "123:abc", "INACAP_USER": "ana",
                   "INACAP_PASS": "nueva"}, "con .env existente no se usa la plantilla"
    assert _parse_env(_with_env("A=1", "", creds("u", "p"))) == \
        {"A": "1", "INACAP_USER": "u", "INACAP_PASS": "p"}, "si faltan, se agregan"
    # Una contraseña puede traer de todo; lo escrito tiene que volver igual.
    for pw in ("a=b", "x#y", " espacios ", '"dobles"', "'simples'", "m\"i'x=#", "ñandú"):
        leido = _parse_env(_with_env(None, plantilla, creds("u", pw)))["INACAP_PASS"]
        assert leido == pw, (pw, leido)
    assert _parse_env("K='v'\nJ=\"w\"\nL=x") == {"K": "v", "J": "w", "L": "x"}
    # Los extras de --setup escriben sus claves con la misma función, sin tocar
    # las credenciales que ya estaban.
    tg = _parse_env(_with_env("INACAP_USER=a\nTELEGRAM_TOKEN=\n", "",
                              {"TELEGRAM_TOKEN": "1:x", "TELEGRAM_CHAT_ID": "9"}))
    assert tg == {"INACAP_USER": "a", "TELEGRAM_TOKEN": "1:x", "TELEGRAM_CHAT_ID": "9"}, tg
    _extras_test()
    _resolve_test()
    _gitignore_test()
    print("Verificaciones OK")


def _extras_test() -> None:
    """--setup extras and --uninstall: everything decided without touching the machine."""
    import tempfile

    # pythonw.exe (the Windows bot task) starts with no stdout/stderr at all.
    guardado = sys.stdout
    sys.stdout = None
    try:
        _utf8_stdio()
    finally:
        sys.stdout = guardado

    # The bot runs at login and must come back if it dies.
    plist = _bot_plist("/v/bin/python", "/R&D/archiver.py")
    assert "<string>com.inacap.bot</string>" in plist, plist
    assert "<string>--bot</string>" in plist and "R&amp;D" in plist, plist
    assert "<key>RunAtLoad</key><true/>" in plist and "<key>KeepAlive</key><true/>" in plist
    assert "StartCalendarInterval" not in plist, "el bot no es una tarea diaria"
    assert "StartCalendarInterval" in _schedule_plist("/p", "/a.py"), "la diaria sigue igual"
    unit = _bot_systemd("/v/bin/python", "/a b/archiver.py")
    assert 'ExecStart="/v/bin/python" "/a b/archiver.py" --bot' in unit, unit
    assert "WantedBy=default.target" in unit and "Restart=" in unit, unit
    xml = _bot_task_xml(r"C:\a b\pythonw.exe", r"C:\R&D\archiver.py", "PC\\José")
    assert r"<Command>C:\a b\pythonw.exe</Command>" in xml, xml
    assert r"&quot;C:\R&amp;D\archiver.py&quot; --bot" in xml, xml
    assert "<UserId>PC\\José</UserId>" in xml, "sin usuario el disparador exige admin"
    assert "<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>" in xml, "si no, lo mata a las 72 h"
    assert "<StopIfGoingOnBatteries>false" in xml, "un notebook sin cargador también"

    # The launcher the installers write, and where --uninstall looks for it.
    home = pathlib.Path("/Users/ana")
    assert _launcher(home, False, "") == home / ".local/bin/inacap"
    assert _launcher(home, True, "") == home / ".local/bin/inacap.cmd"
    assert _launcher(home, False, "/x") == pathlib.Path("/x/inacap"), "respeta XDG_BIN_HOME"

    # Schedules and the bot, per OS. Unloading first stops what is running.
    agents = home / "Library/LaunchAgents"
    cmds, files = _service_teardown("darwin", home)
    assert ["launchctl", "unload", str(agents / "com.inacap.bot.plist")] in cmds, cmds
    assert set(files) == {agents / "com.inacap.bot.plist", agents / "com.inacap.archiver.plist"}
    cmds, files = _service_teardown("win32", home)
    assert cmds.index(["schtasks", "/end", "/tn", "com.inacap.bot"]) < \
        cmds.index(["schtasks", "/delete", "/f", "/tn", "com.inacap.bot"]), "detener antes de borrar"
    assert ["schtasks", "/delete", "/f", "/tn", "com.inacap.archiver"] in cmds and files == []
    cmds, files = _service_teardown("linux", home)
    assert ["systemctl", "--user", "disable", "--now", "inacap-archiver.timer",
            "inacap-bot.service"] in cmds, cmds
    assert len(files) == 3, files
    # Setup stops only the bot (it would swallow the chat-id message): reuse the
    # uninstall teardown, restricted to the bot.
    cmds, files = _service_teardown("darwin", home, bot_only=True)
    assert cmds == [["launchctl", "unload", str(agents / "com.inacap.bot.plist")]], cmds
    assert files == [agents / "com.inacap.bot.plist"], files
    cmds, files = _service_teardown("win32", home, bot_only=True)
    assert ["schtasks", "/delete", "/f", "/tn", "com.inacap.archiver"] not in cmds, cmds
    cmds, files = _service_teardown("linux", home, bot_only=True)
    assert cmds == [["systemctl", "--user", "disable", "--now", "inacap-bot.service"]], cmds
    assert [f.name for f in files] == ["inacap-bot.service"], files

    # Anyone who guesses the bot's name can message it: only private chats are
    # offered, ordered by their latest message, with what they wrote, and names
    # cannot smuggle terminal control sequences.
    updates = {"result": [
        {"message": {"chat": {"id": 1, "type": "private", "first_name": "Ana"}, "text": "hola"}},
        {"message": {"chat": {"id": 9, "type": "group", "title": "Grupo"}, "text": "x"}},
        {"message": {"chat": {"id": 2, "type": "private", "first_name": "Ev\x1b[2Ja\u202e"},
                     "text": "soy\nyo"}},
        {"message": {"chat": {"id": 1, "type": "private", "first_name": "Ana"}, "text": "otra"}},
        {"edited_message": {"chat": {"id": 3, "type": "private", "first_name": "Ed"}}},
    ]}
    assert _private_chats(updates) == [("2", "Ev[2Ja", "soyyo"), ("1", "Ana", "otra")], \
        _private_chats(updates)
    assert _private_chats({"result": []}) == []

    # A requests error message carries the URL, and the URL carries the token;
    # the installer asks students to paste errors into a public issue.
    # Built at run time: a literal token here trips GitHub's secret scanning.
    fake = "123456789:" + "Ab9_-" * 7
    url = f"https://api.telegram.org/bot{fake}/getMe"
    assert fake not in _redact(f"ConnectionError: {url} timed out"), _redact(url)
    assert _redact("Error a las 12:30, código 404") == "Error a las 12:30, código 404"

    # Hints name the short command only when this install's launcher exists.
    assert _command_name("#!/bin/sh\nexec '/h/INACAP-Archiver/.venv/bin/python' "
                         "'/h/INACAP-Archiver/archiver.py' \"$@\"\n",
                         "/h/INACAP-Archiver") == "inacap"
    assert _command_name(None, "/h/INACAP-Archiver") == "python3 archiver.py", "sin lanzador"
    assert _command_name("exec '/otra/archiver.py'", "/h/INACAP-Archiver") == \
        "python3 archiver.py", "el lanzador es de otra instalación"
    os.environ["INACAP_TEST_HOME"] = "/h"
    try:
        assert _command_name('@"%INACAP_TEST_HOME%/INACAP-Archiver/archiver.py" %*',
                             "/h/INACAP-Archiver") == "inacap", "el .cmd usa %USERPROFILE%"
    finally:
        del os.environ["INACAP_TEST_HOME"]

    _uninstall_run_test()

    # Only a directory the installer made is ever deleted whole.
    inst = home / "INACAP-Archiver"
    made = {"archiver.py", "requirements.txt", ".venv", "archive", ".env"}
    assert _installer_dir(inst, home, made)
    assert not _installer_dir(inst, home, made | {".git"}), "un clon de desarrollo, jamás"
    assert not _installer_dir(home, home, made), "nunca la carpeta personal"
    assert not _installer_dir(pathlib.Path("/Users"), home, made), "ni algo que la contenga"
    assert not _installer_dir(inst, home, {"archiver.py", ".env"}), "sin .venv no es del instalador"

    nombres = [".env", "aai.curlrc", "virtual.curlrc", "cookies.txt",
               "manifest.json", ".last-run.json", "archiver.log", "bot.log", ".lock",
               "notebooklm.json", "archive", ".venv", "archiver.py", ".git",
               "README.md", ".env.example"]
    personales = {".env", "aai.curlrc", "virtual.curlrc", "cookies.txt",
                  "manifest.json", ".last-run.json", "archiver.log", "bot.log", ".lock",
                  "notebooklm.json"}
    # `left` is what stays in ROOT; the folder itself goes only if it is empty.
    now, later, left = _uninstall_plan(nombres, wipe=False, delete_archive=False, windows=False)
    assert set(now) == personales, set(now) ^ personales
    assert later == [] and ".git" in left, "de un clon solo salen los datos personales"
    now, _, _ = _uninstall_plan(nombres, wipe=False, delete_archive=True, windows=True)
    assert "archive" in now and ".venv" not in now
    # INACAP_DIR may already hold the student's own files: only ours go.
    sin_git = [n for n in nombres if n != ".git"] + ["__pycache__"]
    ajenos = ["Tarea final.docx", "fotos"]
    now, later, left = _uninstall_plan(sin_git + ajenos, wipe=True, delete_archive=True,
                                       windows=False)
    assert {"archiver.py", "README.md", ".venv", "__pycache__", ".env", "archive"} <= set(now), now
    assert left == ajenos and not set(ajenos) & set(now), "lo ajeno no se toca"
    now, later, left = _uninstall_plan(sin_git, wipe=True, delete_archive=False, windows=False)
    assert "archive" not in now and left == ["archive"], "el material se queda"
    now, later, left = _uninstall_plan(sin_git, wipe=True, delete_archive=True, windows=False)
    assert left == [] and later == [], "nada que conservar: la carpeta queda vacía"
    now, later, left = _uninstall_plan(sin_git, wipe=True, delete_archive=True, windows=True)
    assert ".venv" not in now and later == [".venv"] and left == [], "python.exe bloquea .venv"

    # Windows: .venv goes after this process exits (`timeout` aborts at once
    # without a console, so the wait is a ping). ROOT itself only with a plain
    # rmdir, which refuses a folder that still holds anything.
    raiz = r"C:\Users\José Pérez\INACAP-Archiver"
    cmd = _deferred_removal([raiz + r"\.venv"], [r"C:\Users\José Pérez\.local\bin\inacap.cmd"],
                            raiz)
    assert cmd.startswith('cmd /c "ping -n 3 127.0.0.1 >nul & '), cmd
    assert f'rmdir /s /q "{raiz}\\.venv"' in cmd, cmd
    assert r'del /f /q "C:\Users\José Pérez\.local\bin\inacap.cmd"' in cmd, cmd
    assert cmd.endswith(f'rmdir "{raiz}""') and f'/q "{raiz}"' not in cmd, cmd
    assert "timeout" not in cmd
    assert not _deferred_removal([], ["x"], "").endswith('rmdir ""'), "sin carpeta vacía que borrar"

    # notebooklm-py's own entries only, even under an arbitrary NOTEBOOKLM_HOME,
    # and never in the home folder or above it.
    with tempfile.TemporaryDirectory() as tmp:
        casa = pathlib.Path(tmp)
        nlm = _notebooklm_targets(casa / ".notebooklm", casa)
        assert {t.name for t in nlm} == {"profiles", "config.json", "storage_state.json",
                                         "context.json", "master_token.json",
                                         "browser_profile"}, nlm
        assert all(t.parent == (casa / ".notebooklm").resolve() for t in nlm), nlm
        assert _notebooklm_targets(casa, casa) == [], "NOTEBOOKLM_HOME=~ no borra nada"
        assert _notebooklm_targets(casa / "x" / "..", casa) == [], "se compara ya resuelta"
        assert _notebooklm_targets(casa.parent, casa) == []

    txt = _leftovers("darwin", home)
    assert "/deletebot" in txt and "Drive" in txt, txt
    assert str(home / "Library/Caches/ms-playwright") in txt, txt
    win = pathlib.Path("C:/Users/ana")
    assert str(win / "AppData/Local/ms-playwright") in _leftovers("win32", win)
    assert str(home / ".cache/ms-playwright") in _leftovers("linux", home)


def _utf8_stdio() -> None:
    """Force UTF-8 output, skipping streams that do not exist.

    pythonw.exe, which the Windows bot task runs, starts with stdout and stderr
    set to None; print() then writes nowhere, but reconfigure() would crash.
    """
    for stream in (sys.stdout, sys.stderr):
        if stream is not None:
            stream.reconfigure(encoding="utf-8", errors="replace")


def _uninstall_run_test() -> None:
    """uninstall() end to end, on a temp install: no scheduler is ever invoked."""
    import builtins
    import io
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        home = pathlib.Path(tmp).resolve() / "casa"
        root = home / "INACAP-Archiver"
        for name in (".github", ".venv", "archive"):
            (root / name).mkdir(parents=True, exist_ok=True)
        for name in set(_REPO_FILES) - {".github"} | {".env", "aai.curlrc", "Tarea.docx"}:
            (root / name).write_text("x")
        (home / ".notebooklm" / "profiles").mkdir(parents=True)
        (home / ".notebooklm" / "mio.txt").write_text("x")
        windows = os.name == "nt"
        launcher = _launcher(home, windows, "")
        launcher.parent.mkdir(parents=True)
        launcher.write_text("x")

        calls, spawned, answers = [], [], iter(["si", "s"])
        saved_env = {k: os.environ.pop(k, None) for k in ("NOTEBOOKLM_HOME", "XDG_BIN_HOME")}
        saved_input, builtins.input = builtins.input, lambda prompt="": next(answers)
        try:
            with contextlib.redirect_stdout(io.StringIO()) as out:
                uninstall(root, home, run=calls.append,
                          spawn=lambda cmd, **kw: spawned.append(cmd))
        finally:
            builtins.input = saved_input
            os.environ.update({k: v for k, v in saved_env.items() if v is not None})

        left = {p.name for p in root.iterdir()}
        assert left - {".venv"} == {"Tarea.docx"}, left  # .venv: deferred on Windows
        assert "Tarea.docx" in out.getvalue(), "lo ajeno se nombra"
        assert [p.name for p in (home / ".notebooklm").iterdir()] == ["mio.txt"]
        assert launcher.exists() == windows, "en Windows lo borra el proceso diferido"
        # Never a real detached process: it would hold the temp dir open on Windows.
        assert len(spawned) == windows, spawned
        if windows:
            assert ".venv" in spawned[0] and str(launcher) in spawned[0], spawned
        assert calls and all(c[0] in ("launchctl", "schtasks", "systemctl") for c in calls), calls


class _FakeSession:
    """Records the kwargs of the last get() and answers a canned Location."""

    def __init__(self, location: str):
        self.location, self.kwargs = location, {}

    def get(self, url, **kwargs):
        self.kwargs = kwargs
        return types.SimpleNamespace(headers={"Location": self.location})


def _resolve_test() -> None:
    """resolve() must read the redirect, never walk it off Moodle.

    Following it downloaded a dataset tarball from a slow third-party host and
    hung every run — the daily one and the bot, which polls single-threaded.
    """
    repo = f"https://{REPO_HOST}/repositorio/X/content/index.php?sci=abc"
    s = _FakeSession(repo)
    assert resolve(s, "1") == repo, resolve(s, "1")
    assert s.kwargs["allow_redirects"] is False, s.kwargs
    # Off-site target, and a page that never redirects at all: both are skipped.
    assert resolve(_FakeSession("http://mtg.upf.edu/d/set.tar.gz"), "1") is None
    assert resolve(_FakeSession(""), "1") is None

    # A resolver that raises costs one resource, not every course.
    saved = {n: globals()[n] for n in
             ("discover_courses", "fetch_course", "parse_resources", "resolve")}
    globals().update(
        discover_courses=lambda session: [1],
        fetch_course=lambda session, cid: "<title>Curso: Demo | AAI</title>",
        parse_resources=lambda html: [("1", "url", "roto"), ("2", "resource", "sano")],
        resolve=lambda session, mid: (_ for _ in ()).throw(RuntimeError("host caído")),
    )
    try:
        found = discover(None)
    finally:
        globals().update(saved)
    assert [r["id"] for r in found] == ["2"], found


# Paths git must keep out of the repository, and the one placeholder that must
# stay in. This is not a parser check like the rest: it asks git itself, because
# .gitignore has its own syntax and a plausible-looking rule can silently fail.
# A trailing "# comment" on a pattern line is NOT a comment — it becomes part of
# the pattern — which is exactly how !.env.example once stopped working.
# Rutas de ARCHIVO, no de carpeta: una regla como "archive/" solo casa con
# directorios, y en un clon recién hecho esa carpeta todavía no existe, así que
# git no la reconoce como tal y la comprobación pasaría en falso. Lo que importa
# igual es que no entre lo que hay dentro.
_MUST_IGNORE = (".env", "cookies.txt", "aai.curlrc", "virtual.curlrc",
                "manifest.json", "notebooklm.json", ".last-run.json",
                "archiver.log", "bot.log",
                "archive/Ramo/apunte.pdf", ".atl/skill-registry.md",
                ".DS_Store", "__pycache__/archiver.cpython-311.pyc", ".lock")
_MUST_KEEP = (".env.example", "archiver.py", "notebooklm_sync.py",
              "README.md", "requirements.txt", "LICENSE")


def _gitignore_test() -> None:
    import subprocess

    def ignored(path: str) -> bool:
        # --no-index is essential: without it check-ignore skips paths already
        # in the index, so a tracked file always looks "not ignored" and the
        # check silently passes no matter what the rules say.
        return subprocess.run(["git", "check-ignore", "-q", "--no-index", path],
                              cwd=ROOT, capture_output=True).returncode == 0

    if subprocess.run(["git", "rev-parse", "--git-dir"], cwd=ROOT,
                      capture_output=True).returncode != 0:
        return  # not a clone (a downloaded zip, say) — nothing to check

    for path in _MUST_IGNORE:
        assert ignored(path), f".gitignore dejaría entrar {path} al repositorio"
    for path in _MUST_KEEP:
        assert not ignored(path), f".gitignore está excluyendo {path}"

    # --uninstall deletes the program by this list, never by "everything in ROOT".
    # (Deleted but not yet committed files are still listed by git: skip them.)
    tracked_top = {t.split("/")[0] for t in subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True).stdout.splitlines()
        if (ROOT / t).exists()}
    assert tracked_top == set(_REPO_FILES), tracked_top ^ set(_REPO_FILES)
    # --uninstall in a checkout deletes by _PERSONAL: it must match nothing tracked.
    tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True,
                             text=True).stdout.splitlines()
    top = sorted({t.split("/")[0] for t in tracked})
    borraria = _uninstall_plan(top, wipe=False, delete_archive=True, windows=False)[0]
    assert borraria == [], f"--uninstall borraría archivos versionados: {borraria}"


if __name__ == "__main__":
    # Course names carry accents ("Minería de Datos"). A scheduler redirects
    # stdout to a log file, and a redirected stream uses the locale encoding —
    # cp1252 on a Spanish Windows — so printing a course name would crash the
    # run with UnicodeEncodeError. Force UTF-8; a no-op everywhere else.
    _utf8_stdio()

    ap = argparse.ArgumentParser(
        description="Archivador del campus virtual de INACAP.")
    ap.add_argument("--discover", action="store_true", help="solo lista y resuelve, sin descargar")
    ap.add_argument("--login", action="store_true", help="fuerza ahora el inicio de sesión automático")
    ap.add_argument("--retry-unsupported", action="store_true",
                    help="reintenta las actividades en espera por formato no soportado")
    ap.add_argument("--setup", action="store_true",
                    help="pide y prueba tu usuario y contraseña de INACAP")
    ap.add_argument("--check", action="store_true",
                    help="revisa la instalación y muestra qué falta")
    ap.add_argument("--install-schedule", action="store_true",
                    help="programa la ejecución diaria en este sistema")
    ap.add_argument("--telegram-setup", action="store_true",
                    help="muestra el chat id para poner en el .env")
    ap.add_argument("--bot", action="store_true", help="atiende los comandos de Telegram")
    ap.add_argument("--uninstall", action="store_true",
                    help="quita la ejecución diaria, el bot y tus datos de este equipo")
    ap.add_argument("--self-test", action="store_true", help="verificaciones internas, sin conexión")
    args = ap.parse_args()
    if args.self_test:
        self_test()
    elif args.login:
        print("Sesión iniciada, cookie guardada." if refresh_cookie()
              else f"No se pudo iniciar sesión — corrige tus credenciales con {_cmd()} --setup.")
    elif args.setup:
        setup()
    elif args.check:
        check()
    elif args.install_schedule:
        install_schedule()
    elif args.telegram_setup:
        telegram_setup()
    elif args.bot:
        bot()
    elif args.uninstall:
        uninstall()
    else:
        try:
            summary = run(discover_only=args.discover,
                          retry_unsupported=args.retry_unsupported)
        except Busy:
            sys.exit("Ya hay otra ejecución en curso.")  # not a failure: skip
        except Exception as e:
            report_failure(e)
            raise  # the traceback belongs in the log, where it can be diagnosed
        report_success(summary)
