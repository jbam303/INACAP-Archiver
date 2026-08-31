# 📚 INACAP Archiver

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
- 🗂️ **Organiza por ramo**, con carpetas de nombre legible (`Minería de Datos/`
  en lugar de `TI3061_ASP/`).
- 🧠 **Solo descarga lo nuevo.** Mantiene un registro de lo ya descargado, así
  cada ejecución es rápida y no repite trabajo.
- 🔑 **Renueva la sesión sola.** Cuando la cookie expira, vuelve a iniciar sesión
  por su cuenta y continúa. Sin mantenimiento.
- ☁️ **Respaldo en Google Drive** (opcional), con permisos mínimos: solo puede
  tocar su propia carpeta.
- ⏰ **Se ejecuta solo, todos los días**, mediante un agente de macOS.

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

- **macOS** (el agente que lo ejecuta automáticamente usa `launchd`)
- **Python 3.11 o superior**
- `pip install requests playwright` — `requests` para las descargas, `playwright`
  para el inicio de sesión automático
- **Opcional**: [`rclone`](https://rclone.org) para el respaldo en Drive
  (`brew install rclone`)

---

## 🚀 Instalación

```bash
git clone https://github.com/<tu-usuario>/inacap-archiver.git
cd inacap-archiver
python3 -m pip install requests playwright
```

Luego configura el acceso (más abajo) y estará listo para ejecutarse.

---

## ⚙️ Configuración

### 1. La cookie de sesión (lo mínimo para empezar)

El archivador necesita tu sesión de Moodle. La forma más simple de obtenerla:

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

Ese archivo está en el `.gitignore`, por lo que nunca se sube. Cuando la sesión
expira, el archivador te avisa; o mejor, activa el inicio de sesión automático
(siguiente paso).

### 2. Inicio de sesión automático

Para que renueve la sesión por sí solo, crea un archivo `.env` (también incluido
en el `.gitignore`) con tus credenciales de INACAP:

```
INACAP_USER=tu-correo@inacapmail.cl     # o tu RUT: 12345678-k
INACAP_PASS=tu-contraseña
```

Pruébalo:

```bash
python3 archiver.py --login    # inicia sesión en el formulario real y guarda la cookie
```

Si muestra **"Login OK"**, quedó configurado. A partir de entonces, cada vez que
la sesión caduque, el archivador inicia sesión de nuevo automáticamente. La
contraseña se envía **únicamente** al formulario de INACAP y nunca sale de tu
equipo.

> 🔒 Nota: guardar la contraseña en un archivo local es cómodo, pero queda en
> texto plano. En tu equipo y tu cuenta, el riesgo es bajo. Si esto se usara de
> forma masiva, este es el punto que habría que replantear.

### 3. Google Drive (opcional)

Para respaldar en tu Drive se utiliza `rclone`, que gestiona todo el inicio de
sesión con Google (no hay que programar nada):

```bash
brew install rclone
rclone config       # nuevo remote → nómbralo "gdrive" → tipo "drive"
```

Cuando pregunte por el **scope**, elige `drive.file`. Así rclone **solo puede
ver y modificar los archivos que él mismo sube**; no tiene acceso al resto de tu
Drive. Aunque algo fallara, no podría borrar otros archivos.

Después, en `archiver.py`, activa el respaldo:

```python
DRIVE_REMOTE = "gdrive:INACAP"    # sube a una carpeta "INACAP" en tu Drive
```

Usa `rclone copy`, no `sync`: **solo agrega y actualiza, nunca borra nada** en tu
Drive. Déjalo en `""` para mantenerlo desactivado.

### 4. Bot de Telegram (opcional)

Sirve para dos cosas: recibir un aviso cuando la ejecución diaria encuentra
material nuevo, y pedir una descarga a mano desde el celular.

1. Habla con [@BotFather](https://t.me/BotFather) en Telegram, envía
   `/newbot` y guarda el token que te entrega.
2. Escríbele algo a tu bot recién creado y abre
   `https://api.telegram.org/bot<TOKEN>/getUpdates` en el navegador: ahí aparece
   el `chat.id` de tu conversación.
3. Agrega ambos valores al `.env`:

```
TELEGRAM_TOKEN=123456:ABC-DEF...
TELEGRAM_CHAT_ID=12345678
```

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

## 🖥️ Uso

```bash
python3 archiver.py --discover     # lista lo encontrado, sin descargar
python3 archiver.py                # descarga lo nuevo (+ respaldo a Drive si está activo)
python3 archiver.py --login        # fuerza el inicio de sesión automático
python3 archiver.py --bot          # atiende los comandos de Telegram
python3 archiver.py --self-test    # verificaciones internas, sin conexión
```

Para volver a descargar un recurso, elimina su entrada del `manifest.json`.

---

## ⏰ Ejecución automática diaria

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
  formato sin el `runtime-data.js`. Se reportan como "skipped" y no interrumpen
  la ejecución.
- Los GIFs decorativos de gran tamaño (INACAP sube algunos de ~150 MB como
  "videos") se omiten por defecto, con un límite de 50 MB (`MAX_ASSET_MB` en el
  script).
- Las carpetas vacías se mantienen vacías; cuando el profesor suba material, la
  siguiente ejecución lo descargará.
- El bot depende del equipo: con el Mac apagado no responde. La ejecución diaria
  sí se recupera sola al encenderlo.

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

Elige la que prefieras al publicarlo (MIT es lo habitual para un proyecto así).
Úsalo bajo tu propia responsabilidad: es tu cuenta y tu material.
