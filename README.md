# 📚 INACAP Archiver

[![tests](https://github.com/jbam303/INACAP-Archiver/actions/workflows/tests.yml/badge.svg)](https://github.com/jbam303/INACAP-Archiver/actions/workflows/tests.yml)

Archivador automático para el campus virtual de INACAP (`aai.inacap.cl`).
Descarga el material de tus ramos, lo organiza en tu equipo y, opcionalmente, lo
respalda en tu Google Drive. Se ejecuta una vez al día de forma automática, sin
intervención manual.

La idea es sencilla: los profesores suben material y lo actualizan con
frecuencia, y es fácil olvidar descargarlo o perder algo que luego quitan. Esta
herramienta se encarga de eso por ti: la configuras una vez y se mantiene sola.

> ⚠️ Es tu propio material, de tus ramos, para uso personal y offline.
> Automatizar el acceso puede rozar los términos de uso del campus (lo habitual
> es una limitación de velocidad, no un problema legal). La decisión de usarlo
> es tuya.

---

## ✨ Características

- 🔍 **Descubre tus ramos automáticamente.** Consulta directamente a Moodle qué
  cursos tienes inscritos; no necesitas copiar IDs a mano.
- 📥 **Descarga todo tipo de material**: los Recursos Digitales (paquetes
  interactivos), las presentaciones de asignatura y los archivos que suban
  (PDF, PPTX, DOCX, etc.).
- 📝 **Guarda los talleres y tareas**, tanto el enunciado del docente como el
  archivo que entregaste. Los cuestionarios quedan deliberadamente fuera.
- 🗂️ **Organiza por ramo**, con carpetas de nombre legible (`Minería de Datos/`
  en lugar de `TI3061_ASP/`).
- 🧠 **Solo descarga lo nuevo.** Mantiene un registro de lo ya descargado, así
  cada ejecución es rápida y no repite trabajo.
- 🔑 **Renueva la sesión sola.** Cuando la cookie expira, vuelve a iniciar sesión
  por su cuenta y continúa. Sin mantenimiento.
- ☁️ **Respaldo en Google Drive** (opcional), con permisos mínimos: solo puede
  tocar su propia carpeta.
- ⏰ **Se ejecuta solo, todos los días**, con el programador de tareas del
  sistema (macOS, Linux o Windows).
- 🧠 **Opcionalmente sube todo a NotebookLM**, un cuaderno por ramo, para poder
  preguntarle a tus propios apuntes.

---

## 🧩 Cómo funciona

Lo interesante de este proyecto fue entender cómo está construido el campus por
dentro. Son tres dominios, cada uno con su mecanismo de acceso:

| Dominio | Qué es | Cómo accede |
| --- | --- | --- |
| `aai.inacap.cl` | **Moodle** — el índice de tus ramos | Tu sesión (cookie `MoodleSession`) |
| `virtual.inacap.cl` | El **repositorio** con los Recursos Digitales | Un token `sci` que Moodle entrega en cada enlace |
| `adfs.inacap.cl` | El **inicio de sesión SSO** (SAML) | Usuario y contraseña, sin segundo factor |

El flujo completo:

1. Consulta tus cursos mediante el propio servicio web de Moodle
   (`core_course_get_enrolled_courses_by_timeline_classification`).
2. En cada curso extrae las actividades (`mod/url`, `mod/resource`,
   `mod/folder`) y sigue los redirectores hasta el material real.
3. Los Recursos Digitales son paquetes **Articulate Rise**: todo su contenido
   (texto, imágenes, PDFs adjuntos) viene empaquetado en un `runtime-data.js`
   como JSON en base64. Se decodifica directamente, **sin navegador**, con
   peticiones HTTP.
4. Los archivos sueltos se descargan directamente de Moodle (`pluginfile.php`).
5. Todo se compara contra un manifiesto (`manifest.json`) para descargar
   únicamente lo nuevo.

---

## 📦 Requisitos

- **macOS, Linux o Windows** — cada uno con su programador de tareas
  (`launchd`, `systemd` o el Programador de tareas; ver más abajo)
- **Python 3.9 o superior** (el que traen macOS, Ubuntu y Windows sirve tal cual)
- Dos dependencias, en `requirements.txt`: `requests` para las descargas y
  `playwright` para el inicio de sesión automático
- **Opcional**: [`rclone`](https://rclone.org) para el respaldo en Drive
  (`brew install rclone`, `apt install rclone` o `winget install Rclone.Rclone`)

---

## 🚀 Instalación

```bash
git clone https://github.com/jbam303/INACAP-Archiver.git
cd INACAP-Archiver
python3 -m pip install -r requirements.txt
python3 -m playwright install chromium
cp .env.example .env        # luego edítalo con tus credenciales
```

No te saltes el último paso aunque tengas Google Chrome. El inicio de sesión
automático usa Chrome si puede, pero al ejecutarse desde el programador de
tareas no siempre logra abrirlo, y ahí recurre al Chromium de Playwright. Sin
ese respaldo, la ejecución diaria puede quedarse sin poder renovar la sesión.

Luego configura el acceso (más abajo) y estará listo para ejecutarse.

---

## ⚙️ Configuración

### 1. Tus credenciales (es todo lo que necesitas)

Crea un archivo `.env` (incluido en el `.gitignore`, nunca se sube) con tus
credenciales de INACAP:

```
INACAP_USER=tu-correo@inacapmail.cl     # o tu RUT: 12345678-k
INACAP_PASS=tu-contraseña
```

Listo. En la primera ejecución el archivador inicia sesión solo, guarda la
cookie en `aai.curlrc` y sigue trabajando:

```bash
python3 archiver.py
```

Cuando la sesión caduque, vuelve a iniciarla por su cuenta. No hay que hacer
nada más. Si quieres comprobar solo el inicio de sesión, sin descargar:

```bash
python3 archiver.py --login     # debe responder "Sesión iniciada"
```

La contraseña se envía **únicamente** al formulario de INACAP y nunca sale de tu
equipo.

> 🔒 Nota: guardar la contraseña en un archivo local es cómodo, pero queda en
> texto plano. En tu equipo y tu cuenta, el riesgo es bajo. Si esto se usara de
> forma masiva, este es el punto que habría que replantear.

### 2. La cookie a mano (alternativa, si prefieres no guardar tu contraseña)

El archivador funciona igual si le entregas la sesión tú mismo, sin `.env`:

1. Inicia sesión en `https://aai.inacap.cl` en tu navegador.
2. Abre las herramientas de desarrollo → pestaña **Network** → recarga → clic
   derecho sobre la petición a `my/` → **Copy → Copy as cURL**.
3. De ahí toma los valores de las cookies (`MoodleSession` y, si aparecen,
   `MDL_SSP_SessID`, `MDL_SSP_AuthToken`, `BIGipServerMOODLE`) y colócalos en un
   archivo `aai.curlrc` con este formato:

```
user-agent = "Mozilla/5.0 ..."
cookie = "MoodleSession=xxxx; MDL_SSP_SessID=yyyy; ..."
```

Ese archivo también está en el `.gitignore`. La diferencia es que las sesiones
de Moodle caducan: sin credenciales en `.env` tendrás que repetir estos pasos
cada vez que eso ocurra.

### 3. Google Drive (opcional)

Para respaldar en tu Drive se utiliza `rclone`, que gestiona todo el inicio de
sesión con Google (no hay que programar nada):

```bash
brew install rclone          # macOS · Linux: apt install rclone
                             # Windows: winget install Rclone.Rclone
rclone config       # nuevo remote → nómbralo "gdrive" → tipo "drive"
```

Cuando pregunte por el **scope**, elige `drive.file`. Así rclone **solo puede
ver y modificar los archivos que él mismo sube**; no tiene acceso al resto de tu
Drive. Aunque algo fallara, no podría borrar otros archivos.

Después activa el respaldo en tu `.env`:

```
DRIVE_REMOTE=gdrive:INACAP     # sube a una carpeta "INACAP" en tu Drive
```

Usa `rclone copy`, no `sync`: **solo agrega y actualiza, nunca borra nada** en tu
Drive. Déjalo vacío para mantenerlo desactivado, que es como viene.

### 4. Bot de Telegram (opcional)

Sirve para dos cosas: recibir un aviso cuando la ejecución diaria encuentra
material nuevo, y pedir una descarga a mano desde el celular.

1. Habla con [@BotFather](https://t.me/BotFather) en Telegram, envía
   `/newbot` y guarda el token que te entrega.
2. Agrega el token al `.env` y escríbele cualquier cosa a tu bot en Telegram.
3. Pide el `chat_id` al propio archivador:

```bash
python3 archiver.py --telegram-setup
```

   Responde con la línea lista para pegar:

```
  TELEGRAM_CHAT_ID=12345678       # Tu Nombre
```

4. El `.env` queda así:

```
TELEGRAM_TOKEN=123456:ABC-DEF...
TELEGRAM_CHAT_ID=12345678
```

> Si el bot ya está corriendo como servicio, deténlo antes de pedir el
> `chat_id`: es él quien está recibiendo los mensajes.

Sin estas variables, todo el bloque de Telegram queda desactivado y el
archivador funciona igual que antes.

El bot **solo atiende ese `chat_id`**. Cualquier persona puede escribirle a un
bot de Telegram conociendo su nombre; los mensajes de otros se descartan.

Comandos disponibles:

| Comando   | Qué hace                                              |
| --------- | ----------------------------------------------------- |
| `/bajar`  | Ejecuta una descarga ahora y responde con el resultado |
| `/estado` | Cuántos recursos hay archivados y cuándo fue el último |

---

### 5. NotebookLM (opcional)

Sube el material archivado a [NotebookLM](https://notebooklm.google.com) para
poder preguntarle a tus propios apuntes. Crea **un cuaderno por ramo** y sube
solo los documentos: PDF, DOCX, PPTX y el texto de los Recursos Digitales.

Las imágenes de los paquetes Rise **no** se suben. Su contenido ya viaja dentro
del `content.md` de cada recurso; subirlas gastaría cupo y le metería ruido al
modelo.

```bash
python3 -m pip install "notebooklm-py[browser]"
notebooklm login          # una sola vez, con tu cuenta de Google
python3 notebooklm_sync.py --dry-run    # qué subiría, sin subir nada
python3 notebooklm_sync.py              # súbelo
```

Cada documento pasa por **subir → esperar el procesamiento → renombrar**. El
paso de espera no es decorativo: `source add` termina bien apenas se acepta la
subida, no cuando la fuente quedó lista, así que un archivo que falla al
procesarse igual reportaría éxito. Solo se registra lo que terminó en `ready`;
lo demás se reintenta en la siguiente ejecución.

Los títulos salen del nombre que el docente le puso a la actividad en Moodle,
tomado del `manifest.json`. Nada de `content.md` repetidos.

También extrae las **fuentes citadas dentro del material** —documentación
oficial, glosarios, algún paper— y las agrega como fuentes propias del mismo
cuaderno, para poder consultarlas junto al documento que las cita.

Antes de subir una referencia se comprueba que responda: el material trae
enlaces muertos, y un cuaderno lleno de páginas de error es peor que uno sin
referencias. Con `--sin-referencias` se sube solo el material.

Es **idempotente**: puedes ejecutarlo cuantas veces quieras. Lleva registro en
`notebooklm.json` (ignorado por git) y verifica contra el cuaderno real, así que
si borras una fuente en NotebookLM, la próxima ejecución la repone.

```bash
python3 notebooklm_sync.py --ramo "Big Data"   # un solo ramo
```

> ⚠️ `notebooklm-py` es un proyecto **no oficial** que usa endpoints internos de
> Google, no una API pública. Puede dejar de funcionar sin aviso, y la sesión que
> guarda es la de tu cuenta de Google. Es tu decisión usarlo.

El cupo gratuito de NotebookLM es de **50 fuentes por cuaderno**, y como es por
cuaderno y no en total, un ramo completo entra sin problema.

---

## 🖥️ Uso

```bash
python3 archiver.py --discover     # lista lo encontrado, sin descargar
python3 archiver.py                # descarga lo nuevo (+ respaldo a Drive si está activo)
python3 archiver.py --login        # fuerza el inicio de sesión automático
python3 archiver.py --bot          # atiende los comandos de Telegram
python3 archiver.py --check        # revisa la instalación y muestra qué falta
python3 archiver.py --self-test    # verificaciones internas, sin conexión
python3 archiver.py --retry-unsupported   # reintenta lo marcado como no soportado
python3 archiver.py --install-schedule    # programa la ejecución diaria de las 08:00
python3 archiver.py --telegram-setup      # muestra el chat id para el .env
python3 notebooklm_sync.py                # sube el material a NotebookLM
python3 notebooklm_sync.py --sin-referencias   # solo el material, sin enlaces citados
```

Para volver a descargar un recurso, elimina su entrada del `manifest.json`.

---

## 🩺 Cuando algo no funciona

```bash
python3 archiver.py --check
```

Recorre la instalación completa en orden y muestra dónde se corta:

```
  ✓  Python            Python 3.11.9
  ✓  Dependencias      requests y playwright instalados
  ✗  Credenciales      no existe .env — copia .env.example y complétalo
  ✗  Sesión de Moodle  Todavía no hay sesión de Moodle...
  ·  Google Drive      desactivado (DRIVE_REMOTE vacío en el .env)
  ·  Telegram          desactivado (sin TELEGRAM_TOKEN o TELEGRAM_CHAT_ID)
  ✓  Ejecución diaria  programada a las 08:00
```

El punto (`·`) marca lo que está desactivado a propósito, y no cuenta como
error. Solo la cruz (`✗`) hace que el comando termine con código 1.

Los valores propios salen enmascarados (`22·····4-7`), así que **puedes pegar
esta salida tal cual** al pedir ayuda sin publicar tu RUT ni tu chat.

---

## ⏰ Ejecución automática diaria

Un solo comando, en cualquiera de los tres sistemas:

```bash
python3 archiver.py --install-schedule
```

Detecta tu sistema, escribe la configuración con las rutas absolutas ya
resueltas (tu intérprete de Python y este archivo) y la activa. Si lo ejecutas
de nuevo, reemplaza la anterior sin quejarse.

Debería responder algo así:

```
Programado todos los días a las 08:00 -> /Users/tu-usuario/Library/LaunchAgents/com.inacap.archiver.plist
```

El resto de esta sección explica qué hace ese comando en cada sistema, por si
prefieres instalarlo a mano o revisar lo que quedó.

### macOS (launchd), a mano

Edita las rutas en `com.inacap.archiver.plist` (launchd no expande `~`, así que
necesita rutas absolutas: la de tu Python y la de esta carpeta). Luego:

```bash
cp com.inacap.archiver.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.inacap.archiver.plist
```

Se ejecuta todos los días a las **08:00** y deja el registro en `archiver.log`.
Si el equipo está suspendido a esa hora, se ejecuta al encenderlo.

```bash
launchctl list | grep inacap        # verificar que sigue activo
launchctl unload ~/Library/LaunchAgents/com.inacap.archiver.plist   # desactivarlo
```

### Linux (systemd)

Crea `~/.config/systemd/user/inacap-archiver.service`:

```ini
[Unit]
Description=Archivador INACAP

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 %h/inacap-archiver/archiver.py
StandardOutput=append:%h/inacap-archiver/archiver.log
StandardError=append:%h/inacap-archiver/archiver.log
```

Y `~/.config/systemd/user/inacap-archiver.timer`:

```ini
[Unit]
Description=Archivador INACAP, todos los días a las 08:00

[Timer]
OnCalendar=*-*-* 08:00:00
Persistent=true

[Install]
WantedBy=timers.target
```

`Persistent=true` es el equivalente al comportamiento de launchd: si el equipo
estaba apagado a las 08:00, la ejecución se dispara al encenderlo.

```bash
systemctl --user daemon-reload
systemctl --user enable --now inacap-archiver.timer
loginctl enable-linger "$USER"   # que corra sin sesión abierta
systemctl --user list-timers inacap-archiver.timer   # verificar
```

### Windows (Programador de tareas)

```powershell
schtasks /create /tn "INACAP Archiver" /sc daily /st 08:00 ^
  /tr "pythonw \"%USERPROFILE%\inacap-archiver\archiver.py\""
```

`pythonw` evita que se abra una consola cada mañana. Para que se ejecute cuando
el equipo estaba apagado a esa hora, marca **"Ejecutar la tarea lo antes posible
tras un inicio programado que no se realizó"** en las propiedades de la tarea.

```powershell
schtasks /query /tn "INACAP Archiver"    # verificar
schtasks /delete /tn "INACAP Archiver"   # desactivarlo
```

### El bot como servicio

Si además quieres que el bot atienda comandos siempre que el equipo esté
encendido, instala el segundo agente igual que el anterior (también con rutas
absolutas). Se inicia al entrar a la sesión y launchd lo reinicia si se cae:

```bash
cp com.inacap.bot.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.inacap.bot.plist
```

El registro queda en `bot.log`. **Solo funciona con el equipo encendido**: el
bot mantiene una conexión de consulta saliente hacia Telegram, no hay ningún
servidor escuchando.

En **Linux** es un servicio de usuario con `ExecStart=... archiver.py --bot`,
`Restart=always` y `WantedBy=default.target` (sin temporizador: se mantiene
corriendo). En **Windows**, la misma tarea del punto anterior pero con
`/sc onlogon` en lugar de `/sc daily /st 08:00`, apuntando a `--bot`.

Las dos ejecuciones (la diaria y la de `/bajar`) comparten un archivo de bloqueo
`.lock`, para que nunca se pisen escribiendo el `manifest.json`.

---

## 🗂️ Estructura de carpetas

```
archive/
├── Minería de Datos/
│   ├── U1/TI3061_U1_S1_RD/
│   │   ├── content.md            # el texto de la lección
│   │   └── media/                # imágenes + PDFs de estudio
│   └── Material complementario U1/
│       └── apunte.pptx
├── Big Data/
│   └── 01 Introducción a Big Data/
│       └── 01 Introducción a Big Data.pdf
└── Inglés Intermedio/
    └── AUGUST 13 CLASS/
        └── AUGUST 13 CLASS.docx
```

---

## 🛡️ Seguridad

- Todo lo sensible está en el `.gitignore`: `aai.curlrc`, `.env`, `cookies.txt`
  y tu carpeta `archive/` (tu material no se sube a ningún lado).
- El respaldo a Drive usa el scope `drive.file`: rclone no puede ver ni tocar
  nada fuera de su propia carpeta.
- El respaldo usa `copy`, nunca `sync`: es imposible que borre algo por error.
- Lo que rclone llegara a borrar va a la **papelera de Google** (recuperable
  durante 30 días).

---

## ⚠️ Limitaciones actuales

- Los paquetes **Articulate Storyline** (`story.php`) no se descargan: usan otro
  formato, sin el `runtime-data.js`. Quedan registrados en el manifiesto como
  `unsupported` con su motivo, así dejan de reportarse como nuevos en cada
  ejecución. `--retry-unsupported` vuelve a intentarlos cuando exista soporte.
- Los **cuestionarios** (`mod/quiz`) no se tocan a propósito. Sin un intento
  rendido no hay revisión que archivar, y estos cuestionarios permiten un solo
  intento cronometrado: iniciarlo por accidente sería irreversible.
- Los GIFs decorativos de gran tamaño (INACAP sube algunos de ~150 MB como
  "videos") se omiten por defecto, con un límite de 50 MB (`MAX_ASSET_MB` en el
  script).
- Las carpetas vacías se mantienen vacías; cuando el profesor suba material, la
  siguiente ejecución lo descargará.
- El bot depende del equipo: apagado, no responde. La ejecución diaria sí se
  recupera sola al encenderlo.

---

## 🔭 Mejora futura: llevarlo a Google Cloud Run

Anotado como evolución posible, para dejar de depender de que el equipo esté
encendido. No está implementado.

**Forma correcta — dos piezas, no una:**

- **Cloud Run Job** con el archivador, disparado por Cloud Scheduler para la
  ejecución diaria. Tiene CPU y tiempo propios (hasta 24 h por ejecución).
- **Cloud Run Service mínimo** que solo recibe el webhook de Telegram, invoca el
  job vía API y responde de inmediato. Consume milisegundos de CPU.

Un Service por sí solo **no sirve** para el trabajo pesado: con facturación por
solicitud la CPU se limita apenas se responde, y el webhook de Telegram exige una
respuesta rápida, o reintenta y duplica la descarga.

**Lo que hay que resolver antes:**

1. **Estado.** El contenedor no conserva disco entre ejecuciones. Hay que
   persistir `manifest.json` y `aai.curlrc` en un bucket de Cloud Storage. Sin la
   cookie guardada, Playwright iniciaría sesión en cada ejecución en lugar de
   hacerlo solo cuando expira. El `archive/` no hace falta: ya vive en Drive.
2. **Credenciales.** `INACAP_USER` / `INACAP_PASS` y el token de rclone van a
   Secret Manager, con IAM y registro de auditoría.
3. **El riesgo real: la dirección IP.** Hoy el inicio de sesión en
   `adfs.inacap.cl` sale desde una conexión residencial en Chile. Desde un centro
   de datos cambia la IP y el proveedor; ADFS podría bloquear o marcar la cuenta.
   Conviene la región `southamerica-west1` (Santiago) y probar **solo el inicio
   de sesión** antes de migrar el resto.
4. **Costo.** Prácticamente nulo, pero no exactamente cero: la imagen con
   Chromium supera 1 GB y el almacenamiento en Artifact Registry se cobra por
   encima del nivel gratuito. Verificar precios vigentes al desplegar.

Si se migra, hay que migrar **todo**, incluida la ejecución diaria: mantener
estado en dos lugares hace divergir los manifiestos y se descarga material
repetido.

---

## 🤝 Contribuciones

Si estudias en INACAP y encuentras una mejora, abre un issue o un pull request.
La idea es que le sirva a más estudiantes.

---

## 📄 Licencia

[MIT](LICENSE). Puedes usarlo, modificarlo y compartirlo; solo conserva el aviso
de copyright. Se entrega sin garantía.

Úsalo bajo tu propia responsabilidad: es tu cuenta y tu material.
