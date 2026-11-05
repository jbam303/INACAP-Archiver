# INACAP Archiver installer for Windows (PowerShell 5.1 or later):
#   irm https://raw.githubusercontent.com/jbam303/INACAP-Archiver/main/install.ps1 | iex
# Running it again updates the code; .env, archive\ and manifest.json are not in
# the zip, so they survive. Errors are thrown, never `exit`: under iex, exit
# would close the student's PowerShell window along with the message.
& {
    $ErrorActionPreference = 'Stop'
    $ProgressPreference = 'SilentlyContinue'  # 5.1's progress bar makes downloads crawl

    function Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
    function Check($what) {
        if ($LASTEXITCODE -ne 0) { throw "Falló: $what (código $LASTEXITCODE)." }
    }

    $dir = $env:INACAP_DIR
    if (-not $dir) { $dir = Join-Path $HOME 'INACAP-Archiver' }

    try {
        # The inacap.cmd launcher bakes this path in. cmd.exe reads a .cmd in the
        # OEM code page and expands % even inside quotes, so the part of the path
        # outside the user's profile must be plain ASCII without %. Checked
        # before downloading anything. (Resolved against PowerShell's location,
        # not .NET's current directory, which can differ.)
        $full = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($dir)
        $rest = $full
        $launch = $full
        if ($full.StartsWith($env:USERPROFILE + '\', [StringComparison]::OrdinalIgnoreCase)) {
            # %USERPROFILE% expands to the profile intact, even "José".
            $rest = $full.Substring($env:USERPROFILE.Length)
            $launch = '%USERPROFILE%' + $rest
        }
        if ($rest -match '[^\x20-\x7E]|%') {
            throw "La carpeta $full tiene tildes, ñ o % fuera de tu carpeta de usuario, y el comando inacap no funcionaría ahí. Elige otra con INACAP_DIR (por ejemplo, una dentro de $env:USERPROFILE)."
        }

        Step "Descargando INACAP Archiver en $dir"
        $tmp = Join-Path ([IO.Path]::GetTempPath()) ("inacap-" + [guid]::NewGuid())
        New-Item -ItemType Directory -Path $tmp | Out-Null
        $zip = Join-Path $tmp 'main.zip'
        Invoke-WebRequest -UseBasicParsing -OutFile $zip `
            -Uri 'https://github.com/jbam303/INACAP-Archiver/archive/refs/heads/main.zip'
        Expand-Archive -Path $zip -DestinationPath $tmp
        $src = Get-ChildItem -Path $tmp -Directory | Select-Object -First 1
        New-Item -ItemType Directory -Force -Path $dir | Out-Null
        Copy-Item -Path (Join-Path $src.FullName '*') -Destination $dir -Recurse -Force
        Remove-Item -Recurse -Force $tmp

        $uv = Get-Command uv -ErrorAction SilentlyContinue
        if ($uv) {
            $uv = $uv.Source
        } else {
            Step "Instalando uv (gestor de Python)"
            # Its own process: the uv installer may call exit.
            powershell -NoProfile -ExecutionPolicy ByPass -Command "irm https://astral.sh/uv/install.ps1 | iex"
            Check "instalar uv"
            # This session's PATH does not know about it yet.
            $uv = Join-Path $HOME '.local\bin\uv.exe'
            if ($env:XDG_BIN_HOME) { $uv = Join-Path $env:XDG_BIN_HOME 'uv.exe' }
            if (-not (Test-Path $uv)) {
                throw "uv quedó instalado pero no lo encuentro. Cierra PowerShell, ábrelo y vuelve a ejecutar el comando."
            }
        }

        Push-Location $dir
        try {
            $py = Join-Path $dir '.venv\Scripts\python.exe'

            Step "Preparando Python 3.12 y las dependencias"
            & $uv venv --allow-existing --python 3.12 .venv
            Check "crear el entorno de Python"
            & $uv pip install --python $py -r requirements.txt
            Check "instalar las dependencias"

            Step "Instalando el navegador para el inicio de sesión (puede tardar unos minutos)"
            & $py -m playwright install chromium
            Check "instalar el navegador"

            Step "Creando el comando inacap"
            $bin = $env:XDG_BIN_HOME
            if (-not $bin) { $bin = Join-Path $HOME '.local\bin' }
            New-Item -ItemType Directory -Force -Path $bin | Out-Null
            [IO.File]::WriteAllText((Join-Path $bin 'inacap.cmd'),
                "@`"$launch\.venv\Scripts\python.exe`" `"$launch\archiver.py`" %*`r`n")
            # Puts $bin on PATH for new windows if uv was already installed elsewhere.
            # Not redirected: under 'Stop', 5.1 turns redirected stderr into an error.
            & $uv tool update-shell
            if ($LASTEXITCODE -ne 0) {
                Write-Host "Aviso: no pude agregar $bin al PATH. Si 'inacap' no funciona en una ventana nueva, agrega esa carpeta al PATH a mano." -ForegroundColor Yellow
            }

            Step "Configurando tus credenciales de INACAP"
            & $py archiver.py --setup
            Check "configurar las credenciales"

            Step "Programando la ejecución diaria"
            & $py archiver.py --install-schedule
            Check "programar la ejecución diaria"

            Step "Revisando que todo esté en orden"
            & $py archiver.py --check
            Check "la revisión final"
        } finally {
            Pop-Location
        }

        Write-Host "`nListo. Tu material queda en $dir\archive" -ForegroundColor Green
        Write-Host "`nAbre una ventana nueva de PowerShell y usa:"
        Write-Host "  inacap              baja tu material ahora"
        Write-Host "  inacap --setup      cambia tu contraseña o activa Telegram y NotebookLM"
        Write-Host "  inacap --check      revisa que todo esté en orden"
        Write-Host "  inacap --uninstall  lo quita todo de este equipo"
        Write-Host "Para actualizar, vuelve a ejecutar el mismo comando de instalación."
    } catch {
        Write-Host "`nLa instalación se detuvo: $_" -ForegroundColor Red
        Write-Host "Puedes volver a ejecutar el mismo comando. Si se repite, abre un issue pegando lo que ves:"
        Write-Host "https://github.com/jbam303/INACAP-Archiver/issues"
    }
}
