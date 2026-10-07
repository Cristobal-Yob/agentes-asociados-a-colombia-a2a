# Agentes asociados a Colombia A2A

El agente de correo conversa normalmente y redacta o envía desde **student@example.com** a cualquier destinatario válido indicado por usted. Una orden explícita como «Redacta un recordatorio de la reunión de mañana y envíalo a persona@example.com» compone y envía en ese mismo turno. «Redacta un borrador para persona@example.com» devuelve una vista previa sin enviar. El agente funciona por separado y también mediante A2A con itinerarios.

**Estado:** el código y las pruebas herméticas están preparados para la cuenta personal. Esa cuenta necesita su propio consentimiento OAuth; la autorización anterior de `learner@example.com` no sirve ni se reutiliza. No se ha enviado correo real desde la cuenta personal ni se ha acreditado llegada al buzón. No se iniciaron servicios con este cambio; el perfil personal debe iniciarse después de autorizar la cuenta. Cloud Run sigue pendiente.

Los textos propios están en español; identificadores, contratos, citas y diagnósticos de proveedores conservan su idioma original. El modelo real predeterminado es Vertex en `example-project`, ubicación `global`, modelo `gemini-3.1-flash-lite`. Los errores de credenciales o proveedor nunca activan simulación automáticamente.

## Correo local desde Windows

Autorice una vez su Gmail personal con el [asistente OAuth](docs/gmail-authorization.md). Después, desde este directorio:

```powershell
.\scripts\start_local.ps1
```

En otra terminal, o si el agente personal ya está activo:

```powershell
.\scripts\mail.ps1
```

El cliente conserva `context` automáticamente. Puede conversar, pedir un borrador o dar una orden explícita de redactar y enviar. Si falta destinatario o contenido, pregunta un dato por turno. `cancelar` termina. Pedir un borrador exige revisar la vista previa y responder «Sí, envíalo»; una orden de envío suficiente no exige otro turno. El resultado incluye el mensaje compuesto y la aceptación de Gmail cuando se confirma. Aceptación no significa llegada al buzón.

El inicio usa el intérprete existente, `work/gmail-oauth-personal/authorized-user.json`, la clave existente `work/gmail-oauth/approval-key.txt` y el registro durable `work/gmail-dispatch.sqlite`. Comprueba permisos y puerto 8002, ejecuta en primer plano y no modifica el entorno del llamador. No instala, genera claves ni utiliza el grant anterior. Si falta la autorización personal, falla con un diagnóstico. Un puerto ocupado se rechaza sin detener procesos; el operador debe verificar qué versión está activa antes de usarla.

El cliente envía JSON UTF-8 solo a una URL local y nunca carga credenciales. No reintenta resultados inciertos. La continuación firmada de una orden pendiente se consume una vez mediante reserva durable antes de componer; repetirla no vuelve a generar ni enviar. Los mensajes iniciales sin contexto son solicitudes independientes: repetirlos manualmente puede crear otro correo. No hay deduplicación global por texto ni una medición nueva de rapidez.

## Iniciar el agente real de itinerarios

Requiere Python **3.12 o 3.13**, [uv](https://docs.astral.sh/uv/) y ADC autorizadas para `example-project`. La prueba histórica usó ADC de clase ya existentes. No se carga `.env` automáticamente, no se usan claves de API de Google y la aplicación no cambia la cuenta o el proyecto activo de gcloud. Los siguientes comandos son instrucciones para ejecución humana posterior; esta traducción no los ejecutó.

```powershell
# Windows PowerShell: prepare una vez y ejecute desde este directorio
uv sync --frozen --python 3.12
$env:MODEL_MODE='vertex'; $env:VERTEX_PROJECT_ID='example-project'
$env:VERTEX_LOCATION='global'; $env:MODEL_ID='gemini-3.1-flash-lite'
$env:PUBLIC_AGENT_URL='http://127.0.0.1:8001'
$env:EMAIL_AGENT_URL='http://127.0.0.1:8002'
$env:ENABLE_MCP='false'; $env:PEER_USE_IAM='false'
.\.venv\Scripts\python.exe -m tourism_agent
```

En Bash:

```bash
uv sync --frozen --python 3.12
MODEL_MODE=vertex VERTEX_PROJECT_ID=example-project VERTEX_LOCATION=global MODEL_ID=gemini-3.1-flash-lite PUBLIC_AGENT_URL=http://127.0.0.1:8001 EMAIL_AGENT_URL=http://127.0.0.1:8002 ENABLE_MCP=false PEER_USE_IAM=false .venv/bin/python -m tourism_agent
```

Después de iniciarlo, abra [Swagger de itinerarios](http://127.0.0.1:8001/docs). Ctrl+C detiene el proceso en primer plano. Una solicitud completa usa una llamada pagada y acotada al modelo; saludos y aclaraciones no consultan catálogo ni modelo. El modelo selecciona identificadores suministrados; la aplicación comprueba su pertenencia y construye los hechos. El catálogo curado no reemplaza al modelo real.

Si faltan ADC, obtenga credenciales autorizadas para este proyecto. La aplicación fija el proyecto de Vertex y el de cuota y rechaza otro modelo, proyecto o ubicación. Las credenciales de conectores del navegador no se exportan a estos servicios.

## Autorizar e integrar Gmail personal

El remitente Gmail está fijado a **student@example.com**. Cambiar el texto de la solicitud o `EMAIL_SENDER` no permite usar otra identidad. El grant debe pertenecer a esa cuenta y al cliente de escritorio de `example-project`; cada envío renueva OAuth y verifica email confirmado y `sub`. Los permisos siguen siendo únicamente `gmail.send`, `email` y `openid`. La cuenta de Vertex puede ser diferente. Siga la [guía de autorización](docs/gmail-authorization.md); no copie credenciales a este proyecto ni imprima secretos.

Ambas APIs aceptan `message`; cuando reciben `context`, devuélvalo intacto en el siguiente turno. No hacen falta flags ni consultar al otro agente para integrar correo:

```json
{"message":"Hola, ¿qué puedes hacer?"}
```

```json
{"message":"Redacta un recordatorio de la reunión de mañana y envíalo a persona@example.com"}
```

`persona@example.com` es solo un ejemplo; use el destinatario real solicitado. Si quiere revisar antes de enviar:

```json
{"message":"Redacta un borrador de recordatorio para persona@example.com"}
```

En ese caso revise `preview` y luego envíe `{"message":"Sí, envíalo","context":"CONTEXTO_REAL_DEVUELTO"}`. Una negación, una cita, una instrucción de fuente o una sugerencia del modelo nunca autorizan envío. Las continuaciones de correo están firmadas, ligadas a sesión/configuración y vencen a los 300 segundos. No están cifradas: no las publique. Una cancelación invalida la intención de ese turno sin contactar a Google.

Itinerarios conserva su planificación en el puerto 8001 y decide el handoff A2A dentro de esa llamada. Una petición completa como `{"message":"Bogotá, 2 días, cultura; envíalo a persona@example.com"}` delega redactar y enviar. Pedir solo preparar por correo conserva la vista previa y posterior confirmación. Si la orden inicial de turismo necesitó aclaraciones antes de planificar, la delegación conserva la revisión del borrador; indique «Sí, envíalo» en el siguiente turno. Una visita normal no contacta a correo. No hay servicios de Colombia obligatorios para hablar con el agente de correo.

`/api/email` sigue compatible: `action=draft` devuelve vista previa firmada; `action=send` exige `confirmed=true`, la misma sesión, token y preview sin modificar y vigente. A2A usa JSON tipado del SDK. No se aceptan entradas `TextPart` sin contrato. [examples/email.json](examples/email.json) y [examples/tourism.json](examples/tourism.json) siguen siendo ejemplos de borradores.

`sent` con `delivery=provider_accepted` acredita aceptación registrada por Gmail; `delivery_unknown` informa incertidumbre sin reintentar. El ledger impide un segundo intento automático del mismo borrador o continuación consumida. Una caída después de reservar puede perder un correo todavía no enviado; no se garantiza entrega exactamente una vez. No borre el ledger con aprobaciones vigentes. Cloud Run exige un registro GCS compartido y autorización separada; no usa SQLite efímero.

## Consulta opcional del catálogo en vivo

`ENABLE_MCP=true` habilita API Colombia MCP público cuando la solicitud completa requiere catálogo. Saludos y aclaraciones nunca consultan fuentes. Una prueba corregida obtuvo dos registros de Cartagena; no demuestra disponibilidad continua. El adaptador usa diez segundos y dos herramientas como máximo, declara resultados vacíos, tiempo agotado o indisponibilidad, y puede conservar hechos curados con fechas fijas. Ese respaldo de catálogo no sustituye un modelo en vivo que falla. El contenido de fuentes no autoriza envíos.

Precios, horarios, disponibilidad, accesibilidad, transporte y seguridad siguen sin verificarse. Los planes largos pueden repetir actividades; no se hacen reservas.

## Interfaces y evidencia

| Uso | Ubicación |
|---|---|
| Dos grafos LangGraph y secuencia A2A | [Arquitectura](docs/architecture.md) |
| Contratos de entrada y salida | [Modelos Pydantic](common/contracts.py), [esquemas JSON](docs/contracts/) |
| Descubrimiento y transporte | `/.well-known/agent-card.json` / `/a2a`, A2A 1.0 oficial |
| CI automático y CD manual de las dos APIs | [Guía de GitHub y Cloud Run](docs/ci-cd.md) |

CI comprueba Linux y Windows sin credenciales. CD está preparado para actualizar únicamente `email-agent` y `itinerary-agent` existentes en `example-project/us-central1`, por digest y sin promover tráfico. Necesita WIF, servicios, secretos personales y ledger GCS previamente configurados; los faltantes detienen la ejecución antes de construir. Todavía no se ejecutó en GitHub ni se desplegó. La evidencia histórica privada se conserva fuera del payload publicado.

## Perfil de verificación hermético

Las pruebas eligen `Settings.offline()` o `MODEL_MODE=fixture` / `EMAIL_TRANSPORT=mock` explícitamente. Ese perfil determinista sirve para pruebas y ejercicios sin conexión; no es el valor predeterminado de clase. El harness elimina credenciales heredadas, desactiva proveedores pagados, MCP e IAM, inicia dos procesos propios en puertos locales disponibles y limpia solo esos procesos. Su ejecución es efímera y no deja las APIs reales activas.

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
# Prueba explícita sin conexión y gratuita; nunca envía correo real:
.\.venv\Scripts\python.exe -m scripts.smoke --samples 20 --report ../../work/latency.json
docker compose config --quiet
```

En otros sistemas sustituya `.\.venv\Scripts\python.exe` por `.venv/bin/python`. Los registros temporales van a `work/colombia-runtime`, configurable mediante `COLOMBIA_WORK_DIR`. Ninguna prueba lee `.env` ni llama a un modelo pagado. La latencia histórica del perfil sin conexión no mide Vertex real. Los comandos de instalación, smoke adicional o Docker mostrados aquí no se volvieron a ejecutar durante la traducción.

Compose define exactamente dos contenedores con perfil real. Con Docker disponible, monte explícitamente un archivo ADC autorizado y de solo lectura mediante una configuración local, en la ruta de `GOOGLE_APPLICATION_CREDENTIALS`; nunca se copia ADC del host a la imagen. Correo monta sus credenciales Gmail protegidas como solo lectura, recibe la clave desde el entorno y guarda SQLite en `work/gmail-runtime`. La configuración faltante detiene el inicio. Turismo no depende de correo. No se construyeron ni ejecutaron imágenes aquí. En Cloud Run se usarían ADC de cada cuenta de servicio. La tarjeta de correo del contenedor anuncia `http://email:8080`; en ese perfil use REST/Swagger del host para acceso directo.
