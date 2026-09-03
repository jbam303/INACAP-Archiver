#!/usr/bin/env python3
"""Sube el material archivado a NotebookLM, un cuaderno por ramo.

Se apoya en el CLI de notebooklm-py (proyecto no oficial que habla con
endpoints internos de Google). Requiere `notebooklm login` una vez.

El archivador ya sabe qué bajó; esto sabe qué subió. Son dos estados
distintos y viven en archivos distintos: manifest.json es del archivador y
está protegido por su .lock, así que este script nunca lo escribe.

Uso:
  python3 notebooklm_sync.py                 # todos los ramos
  python3 notebooklm_sync.py --ramo "Big Data"
  python3 notebooklm_sync.py --dry-run       # muestra qué subiría
  python3 notebooklm_sync.py --self-test     # verificaciones sin conexión
"""
from __future__ import annotations

import argparse
import json
import pathlib
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).parent
ARCHIVE = ROOT / "archive"
MANIFEST = ROOT / "manifest.json"
STATE = ROOT / "notebooklm.json"

# What NotebookLM accepts as an uploaded source. The archive's .jpg/.svg/.png
# are assets belonging to a Rise package's content.md, not sources of their own:
# uploading them burns quota and adds noise to a model that already has the
# prose. .zip is not a NotebookLM source type at all.
UPLOADABLE = {".pdf", ".docx", ".pptx", ".md", ".txt", ".csv"}

NOTEBOOK_PREFIX = "INACAP · "
WAIT_TIMEOUT = 180


# --- pure helpers (covered by --self-test) -----------------------------------

def is_uploadable(name: str) -> bool:
    return pathlib.PurePath(name).suffix.lower() in UPLOADABLE


def dest_for(rel: str, dests) -> str | None:
    """The manifest dest that owns `rel`, i.e. the longest one that prefixes it.

    Longest wins because dests nest: a media/ PDF under a Rise package sits
    inside the package's own dest.
    """
    hits = [d for d in dests if rel == d or rel.startswith(d.rstrip("/") + "/")]
    return max(hits, key=len) if hits else None


def title_for(rel: str, names: dict, siblings: int) -> str:
    """The source title for an archived file.

    Moodle's own activity name beats anything we could synthesise: it is what
    the teacher typed. Fall back to the file name when the file predates the
    manifest. When one activity yields several uploadable files, the name alone
    would collide, so the file stem disambiguates — except for content.md,
    which IS the activity (the rest are its attachments).
    """
    path = pathlib.PurePath(rel)
    dest = dest_for(rel, names)
    if dest is None:
        return path.stem
    base = names[dest]
    if path.name == "content.md" or siblings <= 1:
        return base
    return f"{base} — {path.stem}"


def known_ids(state: dict) -> set:
    """Every source id the local record holds.

    --dry-run has no network, so it feeds these to pending() as if they were
    still live. Passing an empty set instead makes every file look pending.
    """
    return {v["source_id"] for v in state.values() if v.get("source_id")}


def pending(rels: list, state: dict, live_ids: set) -> list:
    """Files still to upload for one notebook.

    A recorded upload only counts while its source still exists in the notebook,
    so deleting a source (or the whole notebook) in NotebookLM makes the next
    run put it back instead of silently considering it done.
    """
    return [
        r for r in rels
        if state.get(r, {}).get("source_id") not in live_ids
    ]


def needs_login(output: str) -> bool:
    """True when a CLI failure just means nobody has logged in yet."""
    return "AUTH_REQUIRED" in output or "notebooklm login" in output


# --- state -------------------------------------------------------------------

def load_json(path: pathlib.Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def save_state(state: dict) -> None:
    STATE.write_text(json.dumps(state, indent=2, ensure_ascii=False),
                     encoding="utf-8")


def manifest_names() -> dict:
    """dest -> the activity name Moodle shows, for everything downloaded."""
    return {
        rec["dest"]: rec["name"]
        for rec in load_json(MANIFEST).values()
        if rec.get("dest") and rec.get("name")
    }


# --- the notebooklm CLI ------------------------------------------------------

def _cli_path() -> str:
    exe = shutil.which("notebooklm") or str(
        pathlib.Path(sys.executable).parent / "notebooklm")
    if not pathlib.Path(exe).exists():
        sys.exit("Falta el CLI de notebooklm. Instálalo con:\n"
                 '  python3 -m pip install "notebooklm-py[browser]"')
    return exe


def cli(*args: str, parse_json: bool = False):
    """Run the notebooklm CLI. Returns parsed JSON, or the exit code."""
    argv = [_cli_path(), *args] + (["--json"] if parse_json else [])
    r = subprocess.run(argv, capture_output=True, text=True)
    if parse_json:
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip() or r.stdout.strip())
        return json.loads(r.stdout)
    return r


def ensure_notebook(course: str) -> str:
    """The notebook id for a course, creating it the first time."""
    title = NOTEBOOK_PREFIX + course
    for nb in cli("list", parse_json=True).get("notebooks", []):
        if nb["title"] == title:
            return nb["id"]
    created = cli("create", title, parse_json=True)
    return created.get("id") or created["notebook"]["id"]


def live_source_ids(notebook: str) -> set:
    data = cli("source", "list", "-n", notebook, parse_json=True)
    return {s["id"] for s in data.get("sources", [])}


def upload(path: pathlib.Path, notebook: str, title: str) -> str | None:
    """add -> wait -> rename. Returns the source id, or None if it failed.

    `source add` exits 0 once the upload is accepted, NOT once the source is
    processed: a .pptx that lands in `error` still exits 0. `source wait` is
    the real verdict (0=ready, 1=failed, 2=timeout), so the id is only recorded
    after it passes.
    """
    added = cli("source", "add", str(path), "--type", "file",
                "-n", notebook, parse_json=True)
    source_id = added.get("id") or added.get("source", {}).get("id")
    if not source_id:
        return None
    if cli("source", "wait", source_id, "-n", notebook,
           "--timeout", str(WAIT_TIMEOUT)).returncode != 0:
        return None
    cli("source", "rename", source_id, title, "-n", notebook)
    return source_id


# --- driver ------------------------------------------------------------------

def courses() -> list:
    return sorted(d for d in ARCHIVE.iterdir() if d.is_dir()) if ARCHIVE.exists() else []


def documents(course_dir: pathlib.Path) -> list:
    """Uploadable files under a course, as archive-relative paths."""
    return sorted(
        str(p.relative_to(ARCHIVE))
        for p in course_dir.rglob("*")
        if p.is_file() and is_uploadable(p.name)
    )


def sync(only: str | None, dry_run: bool) -> None:
    if not courses():
        sys.exit("No hay nada archivado todavía. Ejecuta primero:\n"
                 "  python3 archiver.py")
    names = manifest_names()
    state = load_json(STATE)
    total_new = total_failed = 0

    for course_dir in courses():
        course = course_dir.name
        if only and only.lower() not in course.lower():
            continue
        rels = documents(course_dir)
        if not rels:
            continue

        if dry_run:
            # No network here, so trust the local record: treat every id it
            # holds as still live. Passing an empty set would list everything
            # as pending even when it is already uploaded.
            todo = pending(rels, state, known_ids(state))
            print(f"\n{course}: {len(rels)} documento(s), "
                  f"{len(todo)} por subir según el registro local")
            for rel in todo:
                siblings = sum(1 for r in rels
                               if dest_for(r, names) == dest_for(rel, names))
                print(f"  + {title_for(rel, names, siblings)}")
            continue

        try:
            notebook = ensure_notebook(course)
            todo = pending(rels, state, live_source_ids(notebook))
        except RuntimeError as e:
            # The CLI reports a missing session as machine-readable JSON. Shown
            # raw it reads as a crash, so turn it into the one instruction that
            # fixes it. Anything else is a real error and stays visible.
            if not needs_login(str(e)):
                raise
            sys.exit("Falta iniciar sesión en NotebookLM. Ejecuta una vez:\n"
                     "  notebooklm login")
        print(f"\n{course}: {len(rels)} documento(s), {len(todo)} por subir")
        for rel in todo:
            siblings = sum(1 for r in rels
                           if dest_for(r, names) == dest_for(rel, names))
            title = title_for(rel, names, siblings)
            print(f"  subiendo: {title} ...", flush=True)
            source_id = upload(ARCHIVE / rel, notebook, title)
            if source_id:
                state[rel] = {"notebook_id": notebook, "source_id": source_id,
                              "title": title}
                save_state(state)  # persist per file, a crash loses at most one
                total_new += 1
            else:
                print("     falló, quedará pendiente para la próxima")
                total_failed += 1

    if not dry_run:
        print(f"\n{total_new} fuente(s) nueva(s)"
              + (f", {total_failed} fallida(s)." if total_failed else "."))


def self_test() -> None:
    assert is_uploadable("a.PDF") and is_uploadable("x.md")
    assert not is_uploadable("foto.jpg"), "las imágenes de Rise no son fuentes"
    assert not is_uploadable("curso.zip"), "NotebookLM no acepta .zip"

    names = {
        "Minería de Datos/U0/TI3061_U0_PA": "Presentación de la Asignatura",
        "Minería de Datos/U1/TI3061_U1_S1_RD": "Recurso digital: Datos en todas partes",
        "Big Data/01 Introducción": "01 Introducción a Big Data",
    }
    # The longest matching dest wins: a media/ file belongs to its package.
    assert dest_for("Minería de Datos/U1/TI3061_U1_S1_RD/media/g.pdf", names) \
        == "Minería de Datos/U1/TI3061_U1_S1_RD"
    assert dest_for("Otro Ramo/x.pdf", names) is None

    # Moodle's name wins over the file name.
    assert title_for("Big Data/01 Introducción/01 Introducción a Big Data.pdf",
                     names, 1) == "01 Introducción a Big Data"
    # content.md IS the activity, even with attachments beside it.
    assert title_for("Minería de Datos/U1/TI3061_U1_S1_RD/content.md",
                     names, 3) == "Recurso digital: Datos en todas partes"
    # Its attachments need disambiguating, or three sources share one title.
    assert title_for("Minería de Datos/U1/TI3061_U1_S1_RD/media/Glosario.pdf",
                     names, 3) == "Recurso digital: Datos en todas partes — Glosario"
    # Unknown to the manifest: fall back to the file name.
    assert title_for("Otro/suelto.pdf", names, 1) == "suelto"

    # A first-time user has not logged in yet; that must read as an instruction,
    # not as the CLI's raw JSON inside a Python traceback.
    assert needs_login('{"code": "AUTH_REQUIRED", "message": "Auth not found."}')
    assert needs_login("Run 'notebooklm login' first.")
    assert not needs_login("some other failure"), "no confundir otros errores"

    # --dry-run has no network, so it trusts the local record. Passing an empty
    # set here was a real bug: it listed every file as pending even when all of
    # them were already uploaded.
    assert known_ids({"a.pdf": {"source_id": "S1"},
                      "b.pdf": {"source_id": "S2"}}) == {"S1", "S2"}
    assert known_ids({}) == set()
    assert known_ids({"a.pdf": {"title": "sin id"}}) == set(), "entradas sin id"
    dry = {"a.pdf": {"source_id": "S1"}}
    assert pending(["a.pdf", "b.pdf"], dry, known_ids(dry)) == ["b.pdf"]

    state = {"a.pdf": {"source_id": "S1"}, "b.pdf": {"source_id": "S2"}}
    assert pending(["a.pdf", "b.pdf", "c.pdf"], state, {"S1", "S2"}) == ["c.pdf"]
    # A source deleted in NotebookLM must come back on the next run.
    assert pending(["a.pdf", "b.pdf"], state, {"S1"}) == ["b.pdf"]
    assert pending(["a.pdf"], {}, set()) == ["a.pdf"]
    print("Verificaciones OK")


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        _s.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(
        description="Sube el material archivado a NotebookLM, un cuaderno por ramo.")
    ap.add_argument("--ramo", help="sube solo los ramos que contengan este texto")
    ap.add_argument("--dry-run", action="store_true",
                    help="muestra qué subiría, sin subir nada")
    ap.add_argument("--self-test", action="store_true",
                    help="verificaciones internas, sin conexión")
    args = ap.parse_args()

    if args.self_test:
        self_test()
    else:
        sync(args.ramo, args.dry_run)
