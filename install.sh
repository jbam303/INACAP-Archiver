#!/bin/sh
# INACAP Archiver installer for macOS (Linux works too):
#   curl -LsSf https://raw.githubusercontent.com/jbam303/INACAP-Archiver/main/install.sh | sh
# Running it again updates the code; .env, archive/ and manifest.json are not
# in the tarball, so they survive. Everything lives inside main() so that sh
# reads the whole script before running any of it: under `curl | sh` stdin is
# the script itself, and a half-downloaded script must not run halfway.
set -eu

main() {
    dir="${INACAP_DIR:-$HOME/INACAP-Archiver}"
    trap 'rc=$?; [ "$rc" -eq 0 ] || printf "\n✗ La instalación se detuvo (error %s). Lee el mensaje de arriba.\n  Puedes volver a ejecutar el mismo comando. Si se repite, abre un issue pegando lo que ves:\n  https://github.com/jbam303/INACAP-Archiver/issues\n" "$rc" >&2' EXIT

    step "Descargando INACAP Archiver en $dir"
    mkdir -p "$dir"
    tmp="$(mktemp)"
    # No git: on macOS it pops up the Xcode tools installer.
    curl -fsSL -o "$tmp" https://github.com/jbam303/INACAP-Archiver/archive/refs/heads/main.tar.gz
    tar -xzf "$tmp" --strip-components=1 -C "$dir"
    rm -f "$tmp"
    cd "$dir"

    # The uv installer puts it here, but this shell's PATH does not know yet.
    PATH="${XDG_BIN_HOME:-$HOME/.local/bin}:$HOME/.local/bin:$PATH"
    if ! command -v uv >/dev/null 2>&1; then
        step "Instalando uv (gestor de Python)"
        curl -LsSf https://astral.sh/uv/install.sh | sh
    fi
    command -v uv >/dev/null 2>&1 || fail "uv quedó instalado pero no lo encuentro. Cierra la Terminal, ábrela y vuelve a ejecutar el comando."

    step "Preparando Python 3.12 y las dependencias"
    uv venv --allow-existing --python 3.12 .venv </dev/null
    uv pip install --python .venv/bin/python -r requirements.txt </dev/null

    step "Instalando el navegador para el inicio de sesión (puede tardar unos minutos)"
    .venv/bin/python -m playwright install chromium </dev/null

    step "Configurando tus credenciales de INACAP"
    # stdin is the piped script, so questions are read from the terminal.
    .venv/bin/python archiver.py --setup </dev/tty

    step "Programando la ejecución diaria"
    .venv/bin/python archiver.py --install-schedule </dev/null

    step "Revisando que todo esté en orden"
    .venv/bin/python archiver.py --check </dev/null

    printf "\n✓ Listo. El archivo de tu material queda en %s/archive\n" "$dir"
    printf "  Para actualizar o cambiar tu contraseña, vuelve a ejecutar el mismo comando.\n"
}

step() { printf "\n==> %s\n" "$1"; }
fail() { printf "\n✗ %s\n" "$1" >&2; exit 1; }

main "$@"
