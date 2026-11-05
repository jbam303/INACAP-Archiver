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

        Write-Host "`nListo. El archivo de tu material queda en $dir\archive" -ForegroundColor Green
        Write-Host "Para actualizar o cambiar tu contraseña, vuelve a ejecutar el mismo comando."
    } catch {
        Write-Host "`nLa instalación se detuvo: $_" -ForegroundColor Red
        Write-Host "Puedes volver a ejecutar el mismo comando. Si se repite, abre un issue pegando lo que ves:"
        Write-Host "https://github.com/jbam303/INACAP-Archiver/issues"
    }
}
