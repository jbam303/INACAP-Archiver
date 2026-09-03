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
import re
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
USER_AGENT = "Mozilla/5.0 (compatible; inacap-archiver)"
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


# --- referencias citadas en el material --------------------------------------
# Los documentos citan fuentes: documentación oficial, glosarios del NIST, algún
# paper. Vale la pena tenerlas como fuentes propias del cuaderno, para que se
# puedan consultar junto al material que las cita.
#
# Extraer una URL del texto de un PDF la trae sucia. Un PDF no guarda enlaces:
# guarda glifos. De ahí salen las ligaduras tipográficas (ﬁ, ﬂ) y la puntuación
# de la frase pegada al final.

URL_RE = re.compile(r'https?://[^\s"<>\)\]]+')

# Ligaduras que un PDF usa por estética y rompen la URL al extraerla.
_LIGATURES = {"\ufb00": "ff", "\ufb01": "fi", "\ufb02": "fl",
              "\ufb03": "ffi", "\ufb04": "ffl"}

# Hosts que solo aparecen como espacios de nombres XML dentro de un .docx o
# .pptx. Son identificadores con forma de URL, no destinos: nadie los abre.
_SCHEMA_HOSTS = ("schemas.openxmlformats.org", "schemas.microsoft.com",
                 "purl.org", "w3.org", "ns.adobe.com", "iec.ch", "color.org",
                 "sheetjs.openxmlformats.org")

# Ni el Moodle propio (ya está archivado) ni nada local.
_NOT_A_REFERENCE = re.compile(
    r"^https?://(localhost|127\.|0\.0\.0\.0|10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)"
    r"|^https?://[^/]*\b(aai|virtual)\.inacap\.cl", re.IGNORECASE)


def clean_url(raw: str) -> str:
    """Una URL utilizable a partir de lo que se leyó del documento."""
    url = raw.strip()
    for lig, plain in _LIGATURES.items():
        url = url.replace(lig, plain)
    # La frase que la contiene deja su puntuación pegada; la barra final no
    # cambia el destino pero duplicaría la fuente.
    return url.rstrip(".,;:)]}\u2019\"'/")


def is_reference(url: str) -> bool:
    """Si vale la pena sumarla al cuaderno como fuente."""
    if not url.startswith(("http://", "https://")):
        return False
    host = url.split("/")[2].lower() if len(url.split("/")) > 2 else ""
    if any(host == h or host.endswith("." + h) for h in _SCHEMA_HOSTS):
        return False
    return not _NOT_A_REFERENCE.search(url)


def _document_text(path: pathlib.Path) -> str:
    """El texto de un documento, para buscar enlaces dentro.

    Un .docx/.pptx es un zip de XML, y los hipervínculos viven en los .rels, no
    en el texto visible. Un PDF necesita pdftotext; sin él, se lo salta en vez
    de fallar, porque las referencias son un extra y no la tarea principal.
    """
    suffix = path.suffix.lower()
    if suffix in {".md", ".txt", ".csv"}:
        return path.read_text(encoding="utf-8", errors="replace")
    if suffix in {".docx", ".pptx"}:
        import zipfile

        try:
            with zipfile.ZipFile(path) as z:
                return "\n".join(
                    z.read(n).decode("utf-8", "replace") for n in z.namelist()
                    if n.endswith((".xml", ".rels")))
        except Exception:
            return ""
    if suffix == ".pdf" and shutil.which("pdftotext"):
        r = subprocess.run(["pdftotext", "-q", str(path), "-"],
                           capture_output=True, text=True)
        return r.stdout
    return ""


def references_in(paths: list) -> list:
    """Las URLs citadas por un conjunto de documentos, limpias y sin repetir."""
    found = {}
    for path in paths:
        for raw in URL_RE.findall(_document_text(path)):
            url = clean_url(raw)
            if is_reference(url):
                found.setdefault(url, None)
    return list(found)


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


def new_references(refs: list, existing: set) -> list:
    """Las referencias que el cuaderno todavía no tiene, en ningún estado.

    Se compara contra el cuaderno real y no contra el registro local, porque una
    URL que NotebookLM rechazó queda ahí como fuente en `error`: reintentarla
    cada día solo acumularía copias fallidas de lo mismo.
    """
    return [u for u in refs if u not in existing]


def reachable(urls: list, timeout: int = 10) -> list:
    """Las URLs que responden 200, en paralelo.

    Se comprueba antes de subir en vez de dejar que NotebookLM lo descubra: un
    404 puede ingerirse igual y quedar como fuente con el cuerpo del error
    adentro, que es peor que no tener la referencia. Los enlaces muertos del
    material no se registran, así que un reintento futuro los recupera si
    vuelven a vivir.
    """
    import concurrent.futures
    import requests

    def probe(url):
        try:
            r = requests.get(url, timeout=timeout, allow_redirects=True,
                             headers={"User-Agent": USER_AGENT})
            return url if r.status_code == 200 else None
        except Exception:
            return None

    with concurrent.futures.ThreadPoolExecutor(8) as pool:
        return [u for u in pool.map(probe, urls) if u]


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


def live_sources(notebook: str) -> tuple:
    """(ids de fuentes, URLs ya presentes) del cuaderno."""
    data = cli("source", "list", "-n", notebook, parse_json=True).get("sources", [])
    return {s["id"] for s in data}, {s["url"] for s in data if s.get("url")}


def upload_url(url: str, notebook: str) -> str | None:
    """Agrega una referencia como fuente. Sin renombrar: NotebookLM toma el
    título real de la página, que dice más que la URL cruda."""
    try:
        added = cli("source", "add", url, "--type", "url", "-n", notebook,
                    parse_json=True)
    except RuntimeError as e:
        # NotebookLM rechaza algunas páginas por su cuenta (RPC failed). Es una
        # referencia de menos, no una corrida perdida: se omite y se reintenta
        # la próxima vez, igual que un archivo que falla.
        print(f"     lo rechazó NotebookLM: {str(e).splitlines()[-1][:90]}")
        return None
    source_id = added.get("id") or added.get("source", {}).get("id")
    if not source_id:
        return None
    if cli("source", "wait", source_id, "-n", notebook,
           "--timeout", str(WAIT_TIMEOUT)).returncode != 0:
        return None
    return source_id


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


def sync(only: str | None, dry_run: bool,
         sin_referencias: bool = False) -> None:
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
            if not sin_referencias:
                refs = [u for u in references_in([ARCHIVE / r for r in rels])
                        if u not in state]
                for url in refs:
                    print(f"  + (referencia, sin verificar) {url[:66]}")
            continue

        try:
            notebook = ensure_notebook(course)
            ids_vivos, urls_presentes = live_sources(notebook)
        except RuntimeError as e:
            # The CLI reports a missing session as machine-readable JSON. Shown
            # raw it reads as a crash, so turn it into the one instruction that
            # fixes it. Anything else is a real error and stays visible.
            if not needs_login(str(e)):
                raise
            sys.exit("Falta iniciar sesión en NotebookLM. Ejecuta una vez:\n"
                     "  notebooklm login")
        todo = pending(rels, state, ids_vivos)
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

        if sin_referencias:
            continue
        # Las referencias citadas por el material, como fuentes propias. Van al
        # mismo cuaderno a propósito: así se pueden consultar junto al documento
        # que las cita, que es de donde sale el valor.
        refs = new_references(references_in([ARCHIVE / r for r in rels]),
                              urls_presentes)
        if refs:
            vivas = reachable(refs)
            muertas = len(refs) - len(vivas)
            print(f"  referencias citadas: {len(vivas)} accesibles"
                  + (f", {muertas} sin respuesta (se omiten)" if muertas else ""))
            for url in vivas:
                print(f"  subiendo referencia: {url[:70]} ...", flush=True)
                source_id = upload_url(url, notebook)
                if source_id:
                    state[url] = {"notebook_id": notebook, "source_id": source_id,
                                  "title": url, "kind": "referencia"}
                    save_state(state)
                    total_new += 1
                else:
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

    # Las referencias salen del texto de un PDF, donde una URL llega sucia: con
    # la puntuación de la frase pegada, con ligaduras tipográficas y con espacios
    # sobrantes. Sin limpiarla, se sube un enlace roto.
    assert clean_url("https://cvw.cac.cornell.edu/MapReduce/dfs.") == \
        "https://cvw.cac.cornell.edu/MapReduce/dfs", "punto final de la oración"
    assert clean_url("https://ej.com/a),") == "https://ej.com/a"
    assert clean_url("https://csrc.nist.gov/glossary/term/classi\ufb01cation") == \
        "https://csrc.nist.gov/glossary/term/classification", "ligadura fi"
    assert clean_url("https://ej.com/of\ufb02ine") == "https://ej.com/offline"
    assert clean_url("  https://ej.com/x  ") == "https://ej.com/x"
    # Una barra final no cambia el destino, pero duplicaría la fuente.
    assert clean_url("https://spark.apache.org/") == "https://spark.apache.org"

    assert is_reference("https://hadoop.apache.org/docs/")
    assert not is_reference("http://localhost:8888/?token=abc"), "basura de tutorial"
    assert not is_reference("https://127.0.0.1/x") and not is_reference("http://192.168.1.5/")
    assert not is_reference("ftp://ej.com/a"), "solo http y https"
    assert not is_reference("https://aai.inacap.cl/mod/url/view.php?id=1"), \
        "el propio Moodle no es una referencia externa"
    # Un .docx es XML, y el XML declara espacios de nombres con forma de URL.
    # Nadie los visita jamás: son identificadores, no enlaces. Sin filtrarlos,
    # un solo Word aporta 50 "referencias" falsas.
    assert not is_reference("http://schemas.openxmlformats.org/package/2006/relationships")
    assert not is_reference("http://schemas.microsoft.com/office/word/2010/wordml")
    assert not is_reference("http://purl.org/dc/elements/1.1")
    assert not is_reference("http://www.w3.org/2001/XMLSchema-instance")
    assert is_reference("https://www.kdnuggets.com/gpspubs/aimag-kdd-overview-1996-Fayyad.pdf")

    # Una referencia que NotebookLM rechazó queda en el cuaderno como fuente en
    # `error`. Sin mirar lo que ya existe, cada corrida agregaría otra copia
    # fallida de la misma URL, para siempre.
    ya = {"https://a.ej", "https://b.ej"}
    assert new_references(["https://a.ej", "https://c.ej"], ya) == ["https://c.ej"]
    assert new_references([], ya) == []
    assert new_references(["https://a.ej"], set()) == ["https://a.ej"]

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
    ap.add_argument("--sin-referencias", action="store_true",
                    help="no subir las fuentes citadas dentro del material")
    ap.add_argument("--self-test", action="store_true",
                    help="verificaciones internas, sin conexión")
    args = ap.parse_args()

    if args.self_test:
        self_test()
    else:
        sync(args.ramo, args.dry_run, args.sin_referencias)
