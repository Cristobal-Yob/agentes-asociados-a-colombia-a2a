> **Superseded integration proposal.** This server-only helper and its tests remain
> source reference, not a deployed hub handler or a no-send policy. The active
> direction is the [standalone UI](../../demo_ui/README.md) with the hub used only
> for top-level entry and fixed return. Do not implement the old same-origin hub
> proxy diagram below. Nonempty `/api/chat` messages can request real sends.
> Live GET observations below are dated historical evidence, not current acceptance.

# Colombia en el espacio Agentic

Este paquete facilita integrar los agentes existentes en una demo `/explorar/agentic/colombia`, junto a Asbalt. Incluye un cliente **solo de servidor**, sin dependencias ni otro LLM; no modifica ni publica el frontend. [Repositorio](https://github.com/Cristobal-Yob/agentes-asociados-a-colombia-a2a): el despliegue contiene correcciones locales que todavía no están todas publicadas allí.

## Servicios disponibles

| Agente | Documentación | AgentCard | Conversación |
| --- | --- | --- | --- |
| Itinerario | [FastAPI](https://itinerary.example.invalid/docs) | [Card](https://itinerary.example.invalid/.well-known/agent-card.json) | [`POST /api/chat`](https://itinerary.example.invalid/api/chat) |
| Correo | [FastAPI](https://email.example.invalid/docs) | [Card](https://email.example.invalid/.well-known/agent-card.json) | [`POST /api/chat`](https://email.example.invalid/api/chat) |

Docs, Cards y esquemas consultados con GET el 15 de octubre de 2026: HTTP 200. Solo Itinerario y Correo están desplegados para esta integración. La capacidad de Agenda anunciada en la Card no prueba disponibilidad de Calendar. El perfil actual usa Vertex y un catálogo de ejemplo; la consulta nativa por MCP está desactivada. Las sugerencias no verifican disponibilidad ni precios de viajes.

```text
Vista Agentic → handler del mismo origen → Itinerario → Correo por A2A
                                        ↘ Correo directamente
```

El LLM del agente decide si responde, crea el itinerario o delega al otro agente. La interfaz muestra los resultados reales. Las Cards publican capacidades y contacto; los endpoints A2A son `/a2a`, JSON-RPC 1.0. El frontend usa `/api/chat`: no necesita reconstruir A2A ni descubrir herramientas MCP.

## Uso mínimo en el servidor

Configure `COLOMBIA_ITINERARY_API_KEY` y `COLOMBIA_EMAIL_API_KEY` como secretos del servidor. Cada servicio recibe su propia clave mediante `X-API-Key`. Nunca use variables `VITE_*`, claves en HTML, almacenamiento del navegador ni un import de este módulo en React.

```js
import { createTurn, chat } from './client.mjs';

const turn = createTurn('Hola, me llamo Student');
// Save the turn under the authenticated visitor before starting I/O.
const result = await chat('itinerary', turn);
// Save the complete result, including its current plan_version.
const next = createTurn('¿Cómo me llamo?', result);
// Submit next only when the visitor sends that new message.
```

Cambie únicamente `'itinerary'` por `'email'` para hablar directamente con Correo. Ejemplos sin envío: `Crea un itinerario de 3 días en Medellín, solo muéstralo` o `Redacta un correo amable para reagendar una clase; solo necesito la redacción`.

Solicitud ilustrativa, no ejecutada por este paquete:

```json
{"message":"Hola","request_id":"demo-1","session_id":"visitor-1","operation_id":"turn-1"}
```

Respuesta abreviada e ilustrativa de una delegación para redactar, no un recibo real:

```json
{"request_id":"demo-1","session_id":"visitor-1","plan_version":2,"operation_id":"turn-2","status":"completed","message":"Redacción preparada","itinerary":null,"email":{"status":"completed","delivery":"not_attempted","draft":{"subject":"Clase","text":"Propuesta de horario"},"a2a_task_id":"remote-task-id","a2a_context_id":"remote-context-id"}}
```

El cliente devuelve el JSON completo. Muestre `message`, `itinerary`, `draft` o `email.draft`, y el resultado de entrega cuando exista. La evidencia de delegación está en `email.a2a_task_id` y `email.a2a_context_id`; no invente IDs. Un HTTP 200 también puede contener aclaración, error o entrega desconocida. `sent` con `provider_accepted` indica aceptación del proveedor, no llegada al buzón. El remitente configurado es `sender@example.com`.

## Memoria y operaciones

Mantenga `request_id` y `session_id` para toda la conversación; use otros para una conversación nueva y asócielos al visitante autenticado en el servidor. Conserve la última respuesta: `createTurn` reutiliza esos IDs y su `plan_version`, pero crea un `operation_id` nuevo para cada mensaje nuevo. Guarde el turno antes del POST. La memoria durable vive en Firestore; no hace falta interpretar ni reenviar `context`.

No reutilice una operación con otro mensaje ni regenere IDs al perder la conexión. Este cliente hace un solo POST, sin reintentos; limita la espera a 65 segundos y la respuesta a 1 MiB. Un fallo de red, timeout, respuesta inválida o error HTTP se devuelve como `AgentCallError` con `outcome: "unknown"` y la correlación, sin incluir claves ni el contenido remoto.

En ese caso, consulte desde el servidor `GET /api/requests/{request_id}?session_id=...` en el mismo agente, con su clave. Revise el estado durable y la operación original antes de decidir otra acción. No vuelva a mandar automáticamente el POST ni cambie IDs para sortear una reserva pendiente. El cliente no implementa esa recuperación por su cuenta.

Pedir redactar no envía. Una orden humana actual y suficiente de enviar puede ejecutarse sin una segunda aprobación. La interfaz no debe agregar otra llamada a Correo al recibir el resultado de Itinerario: podría duplicar la solicitud. No copie `approval_token` al navegador ni lo registre. Las Cards indican `streaming: false`: muestre espera general mientras llega el resultado y reutilice `thinking`/`done` del robot como estados de la solicitud, sin simular trazas de nodos o A2A en vivo.

## Cambios que debe hacer el dueño del frontend

1. Añadir `/explorar/agentic/colombia` al routing, prerender y catálogo de Agentic, junto a Asbalt. `availableDemo` admite destinos relativos del mismo origen.
2. Añadir un handler estrecho del mismo origen en `deployment/server.mjs`, **antes** del rechazo general de POST. `ssr: false` y las seis rutas prerenderizadas actuales no ofrecen un proxy de conversación. Importar este cliente únicamente allí.
3. Autenticar y autorizar al visitante antes de llamar al agente; vincular sus IDs a su sesión y limitar el uso. **Ocultar la clave no protege un proxy público anónimo de Gmail**. Sin control real de acceso, el handler debe rechazar la operación. Esto no significa pedir otra aprobación por cada correo autorizado.
4. Configurar los secretos de ejecución, incluir los archivos necesarios en las listas de staging/snapshot y comprobar rutas, acceso, memoria, resultados y errores antes de pedir aprobación para una publicación del frontend.

## Verificación y reversión

Pruebas herméticas: `node --test integrations/agentic-front/client.test.mjs`. Usan `fetch` simulado: no consumen LLM ni envían correo. Harness de ejecución real: N/A, este cambio es solo un cliente aislado y documentación. Node instalado verificado: v24.19.0; [fetch y AbortSignal](https://nodejs.org/docs/latest-v24.x/api/globals.html) son APIs integradas.

Para revertir exclusivamente este paquete, retire `README.md`, `client.mjs` y `client.test.mjs` de `integrations/agentic-front/`. No requiere modificar servicios, datos ni el trabajo de otros agentes.
