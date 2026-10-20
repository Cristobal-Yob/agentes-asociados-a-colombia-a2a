> **Public teaching variant.** Runtime defaults are fixture/mock and real mail is
> disabled. The historical Windows operational launchers below now require an
> explicit `COLOMBIA_LIVE_OPT_IN=true`, own model project and own approved Gmail
> identity/project before starting. Their helper commands are not the offline
> student quickstart; use the README tests first. No hosted student access is granted.

# Reanudar solicitudes y proteger cada agente

Cada API conserva su solicitud, versión, propuesta, tareas, aprobaciones y recibos en su propio almacén. El perfil local utiliza SQLite fuera del repositorio; el perfil de Cloud Run utiliza una base Firestore dedicada por servicio. Las rutas de negocio y A2A exigen una clave distinta por servicio. Los registros de efectos de Gmail y Calendar se mantienen separados y siguen decidiendo si un envío o creación ya se intentó.

## Preparar e iniciar en esta PC

Desde la raíz del proyecto, con el intérprete existente:

```powershell
$python = '../../work/colombia-venv/Scripts/python.exe'
& $python -B -m scripts.setup_local --work-root '../../work'
```

El comando crea únicamente las claves ausentes y los directorios privados. Conserva las claves existentes, verifica sus permisos y no inicia OAuth, modelos, APIs ni envíos. No lee ni rota el cliente OAuth, el grant o la clave de aprobación Gmail. Cree una copia privada de seguridad de los archivos de estado y las claves estables antes de una migración.

Abra una terminal por API:

```powershell
.\scripts\start_local.ps1 -Agent email -PythonPath $python
.\scripts\start_local.ps1 -Agent tourism -PythonPath $python
.\scripts\start_agenda.ps1 -PythonPath $python
```

Cada línea inicia un proceso en primer plano; ejecútelas por separado. Los launchers rechazan un puerto ocupado y no detienen procesos anteriores. Agenda puede conversar sin grant Calendar ni credenciales Gmail. El correo opcional requiere su par configurado y activo. Para este entorno, si el intérprete no encuentra datos IANA, el operador puede definir `PYTHONTZPATH` solo en la terminal con el directorio `zoneinfo` del runtime ya disponible; no se instala nada desde los launchers.

| API | Puerto | Clave entrante privada | SQLite de solicitudes | Claves de pares salientes |
|---|---|---|---|---|
| Turismo | 8001 | `work/service-auth/tourism-api-key.txt` | `work/workflows/tourism.sqlite` | Correo y Agenda |
| Correo | 8002 | `work/service-auth/email-api-key.txt` | `work/workflows/email.sqlite` | Ninguna |
| Agenda | 8003 | `work/service-auth/agenda-api-key.txt` | `work/workflows/agenda.sqlite` | Correo, si se solicita |

`work` es el directorio privado hermano de `outputs`, no una carpeta dentro del proyecto. Los perfiles del hijo usan `AGENT_API_KEY_FILE`, `WORKFLOW_DB_PATH` y, según el rol, `EMAIL_AGENT_API_KEY_FILE` y `AGENDA_AGENT_API_KEY_FILE`. También se admiten los valores de entorno sin sufijo `_FILE`; configurar ambas formas es un error. No se cargan archivos `.env` automáticamente.

La clave estable de aprobación Agenda vive en `work/calendar-oauth-clases/approval-key.txt`. Es diferente de las claves API y de la clave Gmail. Después de preparar ese archivo, autorice Calendar **sin** `--signing-key-output`, para conservarlo. Consulte la [guía Calendar](calendar-authorization.md).

## Conversar, confirmar y consultar estado

En Turismo con Vertex, el propio modelo elige automáticamente entre responder y llamar a `generar_itinerario`, `consultar_correo` o `consultar_agenda`. Puede empezar con «Hola», pedir después «Bogotá, un día de cultura» y finalmente «Prepara un borrador para persona@example.com». La última petición reutiliza el plan vigente. También puede pedir generar y enviar en un mismo mensaje completo; únicamente la orden humana actual autoriza el envío mediante los controles de Correo.

La primera respuesta devuelve los IDs. El cliente conserva `request_id`, `session_id` y `plan_version`, actualiza la versión con cada respuesta y los adjunta a los mensajes siguientes. El miniagente ya conserva esos valores en su conversación; la persona escribe solo mensajes. Una solicitud nueva sin IDs abre otra conversación, aunque use la misma clave API o sesión. La clave del servicio no identifica a un estudiante.

Turismo conserva hasta ocho pares de mensajes y 16 000 caracteres en su almacén durable. El modelo recibe texto y una proyección del plan y resultados, sin claves, firmas ni continuaciones opacas. Durante un turno se conservan los pares nativos de llamada/resultado y los metadatos del SDK. El límite es de tres decisiones con herramientas, un plan y una delegación a cada especialista; Agenda conserva la propiedad de su correo final. Un saludo o agradecimiento sin herramientas conserva la versión y la aprobación pendiente, con su vencimiento original. Una revocación explícita retira esa aprobación; un efecto ya iniciado o incierto no se presenta como cancelado ni se reintenta.

Después de generar un plan o recibir el resultado de un especialista, el nodo `respuesta_final` puede resumirlo mediante una única llamada al modelo con herramientas deshabilitadas. Solo reemplaza `message`: conserva el plan, los estados, las firmas privadas y los recibos exactos. No puede delegar otra vez ni autorizar acciones. Ante un fallo, tiempo agotado, llamada a herramienta o afirmación de éxito sin respaldo, devuelve la respuesta original; aceptación del proveedor nunca prueba llegada al buzón. La charla sin herramientas y la recuperación de una llamada rechazada no agregan esta síntesis final.

En Correo con Vertex, el modelo interpreta el mensaje humano actual para conversar, redactar, enviar o cancelar, y elige como contenido el tema actual, el itinerario solicitado o la redacción vigente. «Redacta un correo para reagendar una clase» devuelve `draft` con asunto y texto inmediatamente, sin exigir destinatario ni aprobación y sin enviar; Turismo también puede delegar esa redacción sin plan previo. Una orden posterior como «Envía esa redacción a persona@example.com» utiliza exactamente el texto guardado y envía en ese turno, sin pedir otra aprobación. La aplicación crea y valida su firma técnica internamente: el modelo no aporta destinatarios, firmas ni permisos. El modelo decide la intención y las reservas del mensaje actual; no hay un router de correo por palabras o expresiones regulares. Python valida el esquema, la evidencia literal del humano actual sin citas, el destinatario, las firmas y la idempotencia. Cancelar devuelve un resultado estructurado y retira la autorización pendiente; conserva cualquier recibo anterior aceptado o incierto, incluso si hubo conversación entre ambos turnos. Una respuesta anterior finalizada se reproduce sin ejecutar de nuevo; un resultado incierto nunca se reintenta automáticamente.

El grafo de Correo tiene cuatro nodos: `validate`, `interpret`, `draft` y `send`. La conversación pasa por la decisión del modelo; conversar y cancelar terminan allí, redactar termina con el texto y enviar continúa al ejecutor en el mismo turno. `send` verifica internamente contenido, firma, sesión y vigencia antes del efecto. No hay un nodo de aprobación ni un paso de «OK» obligatorio en la conversación.

Cuando el contenido es un itinerario, Correo conserva el asunto y cuerpo renderizados desde ese plan y evita una generación adicional que no utilizaría. Una redacción libre sigue usando el modelo. Para preguntas sobre un envío anterior, el modelo recibe únicamente su estado, resultado y destinatario original si consta en el recibo; esos datos no autorizan repetirlo. La aceptación del proveedor no demuestra llegada al buzón ni lectura, y un resultado incierto no acredita envío. En una respuesta de conversación, `delivery=not_attempted` describe el turno actual: el recibo histórico sigue disponible en las operaciones de la solicitud.

El perfil `fixture` conserva los ejercicios tipados deterministas sin `message`. Para mensajes naturales exige un modelo programado explícitamente en las pruebas; no interpreta palabras como sustituto del LLM. Las pruebas del flujo nativo inyectan decisiones explícitas; Vertex nunca sustituye un fallo por ese perfil. Las peticiones estructuradas sin `message` conservan su interfaz anterior. La conversación nativa utiliza el mismo campo `message` de respuesta y las mismas rutas HTTP/A2A.

Abra `/docs` en el puerto correspondiente y use **Authorize** con la clave de esa API. `/health`, `/docs`, `/openapi.json` y la AgentCard son públicos; `/api/*` y `/a2a` exigen `X-API-Key`. Una clave ausente, incorrecta o de otro agente produce 401. Un servicio sin clave configurada responde 503 en sus rutas protegidas. La tarjeta describe el esquema; el middleware ejecuta la protección real. IAM opcional conserva su cabecera Bearer independiente.

Una entrada nueva solo necesita el mensaje:

```json
{"message":"Redacta un borrador para persona@example.com con un recordatorio de la reunión."}
```

La respuesta incluye `request_id`, `session_id`, `plan_version` y `operation_id`. Guarde los tres primeros. Incluso después de reiniciar el proceso, puede pedir enviar el borrador disponible sin copiar el contexto:

```json
{"message":"Envía esa redacción a persona@example.com","request_id":"ID_DEVUELTO","session_id":"SESION_DEVUELTA","plan_version":1}
```

Use la versión realmente devuelta. Un borrador guardado es contenido, nunca permiso de envío. Una nueva orden humana explícita puede crear una firma técnica nueva sobre esos bytes. El contrato tipado de bajo nivel `/api/email` conserva sus previews, tokens y vencimiento original de cinco minutos: persistir una autorización no la renueva ni permite reutilizarla alterada. Cambiar contenido o destinatario invalida los vínculos anteriores. Los clientes antiguos pueden devolver el `context` intacto; su huella se vincula a la solicitud y versión durables. No publique contextos o aprobaciones firmadas.

La autorización durable compara el preview y token exactos con la versión vigente antes del núcleo de envío. Etiquetar un preview anterior con una versión u operación nueva no lo vuelve válido; tampoco se puede trasladar una continuación conocida a otra solicitud.

Una operación A2A nueva que no confirma puede sincronizar una revisión superior si Turismo avanzó su plan sin llamar al par. Debe preparar una propuesta nueva: no promueve previews o tokens anteriores, no permite saltos de confirmación y no reabre un resultado `unknown`.

Consulte con la misma clave:

```text
GET /api/requests/ID_DEVUELTO?session_id=SESION_DEVUELTA
```

Devuelve plan vigente, tareas, aprobaciones y operaciones con sus recibos. Cada API consulta únicamente el almacén de su servicio. Las claves no identifican a una persona: son autenticación por servicio. Los identificadores aleatorios de solicitud y la sesión evitan mezclar conversaciones; esta prueba local no implementa cuentas de usuario ni aislamiento multiusuario.

Un bloqueo por Calendar no autorizado queda como `blocked`, nunca como completo. Conserva los datos validados del plan sin aprobar. Después de autorizar, un mensaje nuevo como «Ya autoricé Calendar; continúa» en esa solicitud recupera esos datos, consulta disponibilidad y prepara una propuesta nueva para confirmar. Solo pregunta datos que todavía faltan. Un replay idéntico reproduce el resultado guardado. Para una orden explícita distinta, use otro mensaje con la versión vigente, o inicie una solicitud nueva sin IDs. Repetir una entrada nueva sin IDs constituye otra solicitud y puede producir otro efecto.

## Propiedad de efectos y recuperación

Turismo delega el correo cuando el flujo es Turismo → Correo. En Turismo → Agenda → Correo, Agenda es el único dueño del resumen final; Turismo conserva su recibo y no envía un segundo correo ni un reemplazo ante un resultado incierto. El mismo `request_id` y versión se propagan; cada fase tiene un `operation_id` correlacionable y las respuestas A2A conservan IDs de tarea y contexto.

Después de crear eventos, Agenda delega el mensaje actual a Correo para revisar o enviar su resumen, conservando el plan y los IDs aceptados. El modelo de Correo distingue una redacción de una orden explícita de envío; una revisión de contenido no vuelve a crear eventos. Cambiar fechas o actividades requiere una propuesta Calendar nueva; las evidencias anteriores siguen en el historial.

Una transacción del almacén reserva la operación **antes** de llamar a un proveedor y guarda el recibo **después**. Dos instancias que comparten el almacén de ese servicio no obtienen dos reservas para el mismo turno. No se mantiene una transacción durante la red. Si el proceso cae entre efecto y recibo, la solicitud queda `unknown`; reiniciar o esperar no permite repetirla automáticamente. Puede requerir revisión manual aunque el efecto nunca haya ocurrido. No se promete entrega exactamente una vez.

No borre registros, claves ni aprobaciones para intentar resolver incertidumbre. Los ledgers Gmail/Calendar siguen siendo la autoridad de efectos y no se sustituyen por este estado de conversación. OAuth, claves API/LLM y tokens de proveedor no se guardan en los almacenes de solicitudes; sí contienen mensajes, propuestas y continuaciones privadas. Proteja el directorio completo y sus copias de seguridad.

## Contenedores y límite de alcance

`compose.yaml` admite claves por entorno con nombres separados `TOURISM_API_KEY`, `EMAIL_API_KEY` y `AGENDA_API_KEY`; el operador debe suministrarlas de forma privada. Los volúmenes locales conservan un SQLite por servicio. Un contenedor sin claves falla cerrado. No se ejecutó Docker ni se desplegó infraestructura en esta ampliación.

Un SQLite local, incluso montado en un volumen, no equivale a persistencia distribuida de Cloud Run. Para ese entorno configure explícitamente el SDK oficial Firestore con ADC:

```text
WORKFLOW_STORE=firestore
WORKFLOW_FIRESTORE_PROJECT=example-project
WORKFLOW_FIRESTORE_DATABASE=colombia-a2a-email
```

En Turismo use `WORKFLOW_FIRESTORE_DATABASE=colombia-a2a-itinerary`. No se usa la base `(default)` ni se abre `WORKFLOW_DB_PATH` en ese modo. El perfil local conserva `WORKFLOW_STORE=sqlite` y su ruta habitual. Firestore utiliza transacciones de hasta tres intentos bajo un plazo total de ocho segundos; las lecturas tienen un plazo de tres segundos sin reintentos RPC. Los callbacks solo reservan o guardan datos: nunca llaman modelos, Gmail o Calendar. Un fallo o resultado incierto de almacenamiento no habilita otro envío. Cada operación y recibo se guarda en un documento separado; el historial conserva sus límites locales. Se rechazan documentos mayores que el margen seguro de 900 000 bytes antes de escribirlos.

Antes de cambiar un servicio que ya tiene estado, el operador debe detener sus escrituras y exportar sus filas privadas. La función pura `common.firestore_workflows.sqlite_documents(requests, operations, continuations, histories)` proyecta las filas de SQLite a documentos Firestore sin cambiar IDs, JSON firmados, versiones, sesiones, recibos ni timestamps. No ejecuta una migración. El operador debe crear únicamente documentos ausentes, comparar los existentes y reconciliar una escritura incierta antes de continuar; nunca sobrescribir ni llamar `begin`/`finish` para importar. Las reservas previas continúan inciertas. El ledger Gmail se importa por separado al bucket dedicado `example-project-colombia-a2a-mail-ledger`, conservando sus claves y estados. Cambiar el backend no autoriza repetir operaciones históricas.

Las pruebas usan SQLite real, transacciones Firestore optimistas herméticas y objetos del SDK oficial con transporte RPC sustituido; no acreditan una conexión a Firestore administrado. El despliegue y la prueba de lectura/escritura del almacén real se realizan después con la identidad y recursos autorizados.

Referencias: [transacciones Firestore](https://cloud.google.com/firestore/native/docs/manage-data/transactions), [límites Firestore](https://firebase.google.com/docs/firestore/quotas), [APIKeyHeader de FastAPI](https://fastapi.tiangolo.com/reference/security/) y [APIKeySecurityScheme de A2A](https://a2a-protocol.org/v1.0.1/specification/#452-apikeysecurityscheme).
