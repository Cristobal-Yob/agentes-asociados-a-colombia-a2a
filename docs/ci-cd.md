# CI y revisiones de las dos APIs

El CI se ejecuta en GitHub Actions para cada pull request y cada push a main. El CD se inicia manualmente y prepara revisiones de Cloud Run sin tráfico. El repositorio privado es `Cristobal-Yob/agentes-asociados-a-colombia-a2a`. La autorización OAuth de Gmail personal y la infraestructura de nube requieren la configuración descrita en esta guía.

## Verificación automática

`ci.yml` verifica cada pull request y push a `main` en Linux y Windows con Python 3.12 y uv 0.12.13. Instala `uv.lock` con `--frozen`, comprueba Ruff y formato, ejecuta pytest sin proveedores y regenera los contratos para detectar diferencias con los publicados. Windows ejecuta además `tests/test_local_scripts.ps1` con el intérprete del entorno creado por uv.

Las pruebas usan fixtures, transportes simulados y directorios temporales. No necesitan ADC, OAuth, Secret Manager ni acceso a modelos. Sus resultados no acreditan envío real. Las acciones externas están fijadas a commits verificados de sus repositorios oficiales.

## Preparar CD manual

`cd-cloud-run.yml` solo acepta `workflow_dispatch` desde `main` del repositorio aprobado. Repite CI antes de autenticar el job de revisiones y usa OIDC mediante Workload Identity Federation. Configure estas **variables de GitHub** después de crear el repositorio:

| Variable | Valor que debe proporcionar el administrador |
|---|---|
| `EXPECTED_GITHUB_REPOSITORY` | `Cristobal-Yob/agentes-asociados-a-colombia-a2a` |
| `GCP_WORKLOAD_IDENTITY_PROVIDER` | Recurso completo `projects/NUMERO/locations/global/workloadIdentityPools/POOL/providers/PROVIDER` existente y aprobado |
| `GCP_DEPLOY_SERVICE_ACCOUNT` | Cuenta de despliegue separada, existente, del proyecto `example-project` |

Prepare el entorno GitHub `cloudrun-two-agents` con aprobación humana y restricción a `main`. El proveedor WIF debe restringir el repositorio exacto, propietario, rama `refs/heads/main` y entorno `cloudrun-two-agents`, mediante claims mapeados y condición de atributos; no confíe en un pool abierto a cualquier repositorio. La cuenta de despliegue necesita `roles/iam.workloadIdentityUser` para ese principal restringido. El workflow no configura esas políticas ni crea identidades.

El job concede únicamente `contents: read` e `id-token: write`. No guarde claves JSON, ADC, grants Gmail ni la clave de firma en GitHub Secrets. La acción de autenticación crea credenciales temporales `gha-creds-*.json`, excluidas de Git y del contexto Docker, y las elimina al terminar.

## Recursos que deben existir antes de construir

El preflight lee configuración y metadatos, sin acceder al contenido de los secretos. Si falta un recurso, permiso o configuración, termina antes de Docker y antes de modificar Cloud Run. El administrador debe preparar y verificar por separado:

| Recurso | Configuración requerida |
|---|---|
| Proyecto y región | `example-project`, `us-central1`; facturación y APIs necesarias habilitadas |
| Artifact Registry | Repositorio Docker `example-agents` en `us-central1` |
| Correo | Servicio existente y listo `email-agent`, runtime `email-agent@example-project.iam.gserviceaccount.com` |
| Itinerarios | Servicio existente y listo `itinerary-agent`, runtime `itinerary-agent@example-project.iam.gserviceaccount.com` |
| Ambos runtimes | 1 CPU, 1 GiB, mínimo 0 y máximo 1 instancia, concurrencia 8, timeout 45 s, CPU limitada fuera de solicitudes, ingreso `all` con invocación IAM habilitada |
| Modelo | `MODEL_MODE=vertex`, proyecto `example-project`, ubicación `global`, `gemini-3.1-flash-lite`, ADC del runtime; sin claves de API |
| URLs | `PUBLIC_AGENT_URL` igual a la URL canónica HTTPS de cada servicio; itinerarios con `EMAIL_AGENT_URL` igual a correo, `PEER_USE_IAM=true`, `ENABLE_MCP=false` |
| Gmail | `EMAIL_TRANSPORT=gmail`, `EMAIL_SENDER=student@example.com`, `ENABLE_REAL_EMAIL=true`; grant propio de esa cuenta, autorizado con el cliente aprobado de clases |
| Secret Manager | Versiones numéricas habilitadas de `example-gmail-oauth` montada en `/secrets/gmail/authorized-user.json` y `email-agent-approval` referenciada por `APPROVAL_SIGNING_KEY` |
| Idempotencia durable | `GMAIL_LEDGER=gcs`, `GMAIL_LEDGER_BUCKET=example-project-email-agent-ledger`; bucket dedicado existente con acceso uniforme, prevención pública y `roles/storage.objectUser` para el runtime de correo |
| Acceso privado | Sin bindings públicos en proyecto, servicios o bucket; itinerarios tiene `roles/run.invoker` explícito sobre correo |
| Tráfico existente | Asignaciones fijadas a nombres concretos de revisiones, sumando 100 %, sin selector `LATEST` |

El backend GCS ya implementado reserva y completa objetos mediante generaciones condicionales. No se monta SQLite sobre Cloud Storage FUSE. SQLite local o el disco efímero de Cloud Run no cumplen este requisito. Mantenga la misma clave de aprobación y ledger para preservar reservas vigentes. El preflight comprueba referencias y versiones habilitadas; la identidad OAuth real solo la verifica el runtime al renovar el grant. No puede acreditar consentimiento o entrega leyendo metadatos.

La cuenta de despliegue necesita permisos acotados: lectura de servicios/revisiones y políticas IAM de esos servicios; actualización de esos dos servicios; escritura y lectura de imágenes solo en ese repositorio; `iam.serviceAccounts.actAs` sobre ambos runtimes; lectura de metadatos y políticas del bucket; lectura de metadatos de las dos versiones de secretos, y `resourcemanager.projects.getIamPolicy` para descartar acceso público heredado. Puede usar roles personalizados para acotar las lecturas; no se necesita `secretmanager.versions.access`, Editor, Owner, creación de recursos, modificación IAM ni Cloud Build. Los runtimes necesitan sus permisos de Vertex, Secret Manager y GCS ya configurados. Este repositorio no concede permisos ni habilita APIs.

## Qué hace una ejecución aprobada

1. Valida repositorio, rama, identidad WIF y cuenta de despliegue antes de autenticar. Ejecuta CI y comprueba todos los prerrequisitos existentes antes de construir.
2. Copia únicamente `pyproject.toml`, `uv.lock`, módulos Python de los tres paquetes y los dos Dockerfiles a un staging externo. Rechaza enlaces simbólicos, junctions y rutas escapadas. No copia credenciales, pruebas, documentación ni evidencias al contexto de build.
3. Construye los dos Dockerfiles en el runner y sube imágenes con etiqueta única de commit, ejecución e intento. Resuelve el digest real desde Artifact Registry y vuelve a comprobar la configuración antes de actualizar.
4. Ejecuta `gcloud run services update` exclusivamente con `--image ...@sha256:...` y **`--no-traffic`**. No configura IAM, variables, secretos, audiencia, escalado ni etiquetas de tráfico. Comprueba configuración conservada, asignaciones de tráfico, revisión lista y digest exacto.

Las nuevas revisiones no reciben tráfico del servicio. El workflow no hace pruebas funcionales con Gmail o Vertex, no publica tags de revisión y no promueve tráfico automáticamente. Una promoción posterior exige revisión de los dos resultados y autorización separada. No interprete la URL normal del servicio como prueba de la nueva revisión. Los builds, almacenamiento de imágenes y posibles arranques de revisión pueden generar costos; se conservan los límites existentes, sin prometer costo cero.

Si falla después de preparar una de las revisiones, puede quedar una actualización parcial sin tráfico. No hay reintentos de despliegue ni rollback automáticos. La salida exitosa muestra servicio, revisión y digest; no incluye secretos. Ante un error se detiene con diagnóstico acotado. Conserve fuera del repositorio el mapa previo de tráfico y los digests anteriores para que un administrador pueda decidir una reversión explícita. No elimine secretos o ledger para revertir código.

Fuentes oficiales: [WIF para GitHub Actions](https://github.com/google-github-actions/auth), [setup-gcloud](https://github.com/google-github-actions/setup-gcloud), [uv en CI](https://docs.astral.sh/uv/guides/integration/github/), [actualización de Cloud Run](https://docs.cloud.google.com/sdk/gcloud/reference/run/services/update), [revisiones y tráfico](https://docs.cloud.google.com/run/docs/rollouts-rollbacks-traffic-migration).
