#!/usr/bin/env python3
"""INACAP virtual-campus archiver.

Discovers the Digital Resources ("Recursos Digitales") listed in the user's
Moodle courses, then downloads new ones (text + media + attached PDFs) into a
local tree. Pure HTTP, no browser. Designed to run unattended on a daily
schedule; the only manual chore is refreshing the Moodle session cookie in
aai.curlrc when it expires.

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
  python3 archiver.py --bot            # serve Telegram commands (long-poll)
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

# Optional Google Drive sync. Set to an rclone remote:path (e.g.
# "gdrive:INACAP") after running `rclone config` once; empty disables it.
# See the README. rclone handles OAuth, token refresh and incremental upload.
DRIVE_REMOTE = "gdrive:INACAP"


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
    """Read the `cookie = "..."` line from a gitignored curlrc file."""
    path = ROOT / curlrc
    if not path.exists():
        sys.exit(f"Missing {curlrc} — export the session cookie into it (see README).")
    m = re.search(r'cookie\s*=\s*"(.*)"', path.read_text())
    if not m:
        sys.exit(f"No cookie line found in {curlrc}.")
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
    """
    import fcntl

    with open(LOCK, "w") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise Busy("another run is in progress")
        yield


def make_session():
    import requests

    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    s.headers["Cookie"] = load_cookie("aai.curlrc")
    return s


def _load_env() -> dict:
    """Parse KEY=VALUE lines from a gitignored .env (no dependency needed)."""
    path = ROOT / ".env"
    env = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


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

    print("Refreshing Moodle session (auto-login) ...", flush=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
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
    """Follow a /mod/url redirector to its final virtual.inacap.cl URL."""
    r = session.get(
        f"{MOODLE}/mod/url/view.php?id={moodle_id}&redirect=1", timeout=40
    )
    return r.url if REPO_HOST in r.url else None


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
                url = resolve(session, moodle_id)
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
    return json.loads(MANIFEST.read_text()) if MANIFEST.exists() else {}


def save_manifest(manifest: dict) -> None:
    MANIFEST.write_text(json.dumps(manifest, indent=2, ensure_ascii=False))


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
_TEXT_KEYS = {
    "description", "title", "text", "caption", "heading", "paragraph",
    "content", "label", "altText", "body", "term", "definition",
    "question", "answer",
}
_DOC_RE = re.compile(r"\.(pdf|docx?|pptx?|xlsx?|zip)$", re.IGNORECASE)


def _strip_html(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(s))).strip()


def _lesson_text(node) -> list[str]:
    """Collect deduped prose strings from a lesson subtree, in document order."""
    out: list[str] = []
    seen: set[str] = set()

    def walk(o, key=None):
        if isinstance(o, dict):
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
    (dest / "content.md").write_text(text)

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
                    "auto-login failed — check INACAP_USER/INACAP_PASS in .env")
        else:
            raise SessionExpired(
                "refresh aai.curlrc, or set INACAP_USER/INACAP_PASS in .env "
                "for auto-login")
    manifest = load_manifest()
    new = pending(resources, manifest, retry_unsupported)
    parked = sum(1 for v in manifest.values() if "unsupported" in v)

    print(f"{len(resources)} activities found, {len(new)} new"
          + (f", {parked} parked (unsupported)." if parked else "."))
    for r in new:
        loc = str(dest_for(r).relative_to(ARCHIVE))
        print(f"  [new] ({r['kind']}) {r['name']}  ->  {loc}")

    if discover_only:
        return ""

    saved = []
    for r in new:
        dest = dest_for(r)
        print(f"Downloading: {r['name']} ...", flush=True)
        result = download(session, r, dest)
        if "skipped" in result:
            print(f"  parked: {result['skipped']}")
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
            note = f"  saved {result['chars']} chars, {len(result['media'])} media files"
            if result.get("oversize"):
                note += f" ({len(result['oversize'])} oversize skipped)"
            print(note)
        else:
            print(f"  saved {len(result['files'])} file(s)")

    sync_to_drive()
    return summarize(saved)


def _tool_path(env_path: str) -> str:
    """Where to look for external tools like rclone.

    launchd (and cron) hand a job a minimal PATH — /usr/bin:/bin:/usr/sbin:/sbin —
    with no Homebrew in it, so `which rclone` fails under the daily agent even
    though it works in a terminal. Search the usual Homebrew prefixes too.
    """
    return ":".join(p for p in (env_path, "/opt/homebrew/bin", "/usr/local/bin") if p)


def sync_to_drive() -> None:
    """Mirror the archive to Google Drive via rclone, if configured.

    ponytail: rclone already does Drive OAuth, token refresh and incremental
    upload — no reason to hand-roll the Drive API. `copy` (not `sync`) never
    deletes anything on Drive, matching this tool's append-only archive.
    """
    if not DRIVE_REMOTE:
        return
    import shutil
    import subprocess

    rclone = shutil.which("rclone", path=_tool_path(os.environ.get("PATH", "")))
    if not rclone:
        print("Drive sync skipped: rclone not installed (see README).")
        return
    if not ARCHIVE.exists():
        return
    print(f"Syncing to Drive ({DRIVE_REMOTE}) ...", flush=True)
    rc = subprocess.run(
        [rclone, "copy", str(ARCHIVE), DRIVE_REMOTE, "--fast-list"]
    ).returncode
    print("  Drive sync done." if rc == 0 else f"  Drive sync failed (rclone exit {rc}).")


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
    import requests

    token, default_chat = conf
    if len(text) > _MAX_MESSAGE:
        text = text[:_MAX_MESSAGE] + "\n… (recortado)"
    try:
        requests.post(
            _API.format(token=token, method="sendMessage"),
            json={"chat_id": chat_id or default_chat, "text": text},
            timeout=30,
        )
    except Exception as e:  # a failed notification must never sink a run
        print(f"Telegram notify failed: {e}")


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
        sys.exit("Set TELEGRAM_TOKEN and TELEGRAM_CHAT_ID in .env (see README).")
    import requests

    token, owner = conf
    offset = 0
    print("Bot listening (ctrl-C to stop).", flush=True)
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
                sys.exit(f"Telegram rejected the poll: {payload.get('description')}")
            updates = payload["result"]
        except Exception as e:
            print(f"poll failed: {e}", flush=True)
            time.sleep(15)  # network hiccup or Telegram blip; back off and retry
            continue
        for update in updates:
            offset = update["update_id"] + 1
            cmd, chat = parse_command(update)
            # Anyone can message a bot: only the owner's chat is ever served.
            if not cmd or chat != owner:
                continue
            print(f"command: {cmd}", flush=True)
            if cmd == "/bajar":
                notify("Buscando novedades…", chat)
            notify(handle(cmd), chat)


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

    # A parked (unsupported) activity must stop counting as new, or the daily log
    # cries wolf forever — but --retry-unsupported must still pick it back up.
    res = [{"id": "1"}, {"id": "2"}, {"id": "3"}]
    man = {"2": {"downloaded_at": "2026-08-31T08:00:00"}, "3": {"unsupported": "not Rise"}}
    assert [r["id"] for r in pending(res, man)] == ["1"], pending(res, man)
    assert [r["id"] for r in pending(res, man, retry_unsupported=True)] == ["1", "3"]
    assert pending(res, {}) == res, "an empty manifest leaves everything pending"

    assert summarize([]) == "", "nothing new must produce no message"
    msg = summarize([
        {"course": "Minería de Datos", "name": "Recurso digital: Clustering"},
        {"course": "Big Data", "name": "Clase 3"},
    ])
    assert msg.startswith("INACAP: 2 recurso(s) nuevo(s)"), msg
    assert "• [Big Data] Clase 3" in msg, msg

    upd = {"update_id": 7, "message": {"chat": {"id": 12345}, "text": "/Bajar@mibot ya"}}
    assert parse_command(upd) == ("/bajar", "12345"), parse_command(upd)
    assert parse_command({"message": {"chat": {"id": 1}, "text": "hola"}}) == ("", "1")
    assert parse_command({"edited_message": {}}) == ("", "")  # nothing to serve

    # launchd's minimal PATH must still find Homebrew tools.
    assert _tool_path("/usr/bin:/bin") == "/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin"
    assert _tool_path("").startswith("/opt/homebrew/bin"), _tool_path("")
    print("self-test OK")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--discover", action="store_true", help="list + resolve only")
    ap.add_argument("--login", action="store_true", help="force cookie auto-login now")
    ap.add_argument("--retry-unsupported", action="store_true",
                    help="re-attempt activities parked as unsupported")
    ap.add_argument("--bot", action="store_true", help="serve Telegram commands")
    ap.add_argument("--self-test", action="store_true", help="offline parser checks")
    args = ap.parse_args()
    if args.self_test:
        self_test()
    elif args.login:
        print("Login OK, cookie saved." if refresh_cookie()
              else "Login failed — check INACAP_USER/INACAP_PASS in .env.")
    elif args.bot:
        bot()
    else:
        try:
            notify(run(discover_only=args.discover,
                       retry_unsupported=args.retry_unsupported))  # silent when nothing is new
        except Busy:
            sys.exit("Another run is in progress.")
        except SessionExpired as e:
            notify(f"INACAP: la corrida diaria falló. Sesión de Moodle: {e}")
            sys.exit(f"Moodle session expired — {e}.")
