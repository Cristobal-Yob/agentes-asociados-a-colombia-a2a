"""Prepara revisiones de dos servicios existentes; nunca promueve tráfico ni crea recursos."""

import argparse
import copy
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

PROJECT = "example-project"
REGION = "us-central1"
REPOSITORY = "example-agents"
BUCKET = f"{PROJECT}-email-agent-ledger"
SERVICES = {"email-agent": "email", "itinerary-agent": "tourism"}
IMAGE_ROOT = f"{REGION}-docker.pkg.dev/{PROJECT}/{REPOSITORY}"


class ReleaseError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise ReleaseError(message)


def validate_request(env):
    expected = env.get("EXPECTED_GITHUB_REPOSITORY", "")
    require(
        re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", expected)
        and env.get("GITHUB_REPOSITORY") == expected
        and env.get("GITHUB_REF") == "refs/heads/main"
        and env.get("GITHUB_EVENT_NAME") == "workflow_dispatch",
        "Se requiere ejecución manual desde main en el repositorio aprobado.",
    )
    require(
        re.fullmatch(r"[0-9a-f]{40}", env.get("GITHUB_SHA", ""))
        and re.fullmatch(r"[1-9][0-9]*", env.get("GITHUB_RUN_ID", ""))
        and re.fullmatch(r"[1-9][0-9]*", env.get("GITHUB_RUN_ATTEMPT", "")),
        "Identidad de ejecución incompleta.",
    )
    require(
        re.fullmatch(
            r"projects/[0-9]+/locations/global/workloadIdentityPools/"
            r"[a-zA-Z0-9_-]+/providers/[a-zA-Z0-9_-]+",
            env.get("GCP_WORKLOAD_IDENTITY_PROVIDER", ""),
        ),
        "Configure GCP_WORKLOAD_IDENTITY_PROVIDER con el recurso WIF aprobado.",
    )
    account = env.get("GCP_DEPLOY_SERVICE_ACCOUNT", "")
    require(
        re.fullmatch(
            r"[a-z][a-z0-9-]{4,28}[a-z0-9]@example-project\.iam\.gserviceaccount\.com", account
        )
        and account not in {f"{service}@{PROJECT}.iam.gserviceaccount.com" for service in SERVICES},
        "Configure una cuenta de despliegue separada en example-project.",
    )
    return f"{env['GITHUB_SHA']}-{env['GITHUB_RUN_ID']}-{env['GITHUB_RUN_ATTEMPT']}"


def ready(raw):
    return any(
        item.get("type") == "Ready" and item.get("status") == "True"
        for item in raw.get("status", {}).get("conditions", [])
    )


def traffic(raw):
    entries = raw.get("spec", {}).get("traffic", [])
    require(
        entries
        and all(item.get("revisionName") and not item.get("latestRevision") for item in entries)
        and sum(int(item.get("percent", 0)) for item in entries) == 100,
        "El tráfico debe estar fijado previamente a revisiones explícitas, sin LATEST.",
    )
    return sorted(
        (item["revisionName"], int(item.get("percent", 0)), item.get("tag", "")) for item in entries
    )


def secret_ref(ref, expected):
    require(
        ref.get("name") == expected and re.fullmatch(r"[1-9][0-9]*", str(ref.get("key", ""))),
        f"Se exige referencia a una versión numérica de {expected}.",
    )
    return expected, str(ref["key"])


def validate_service(raw, name, email_url=None):
    require(
        name in SERVICES and raw.get("metadata", {}).get("name") == name, "Servicio no permitido."
    )
    metadata = raw["metadata"]
    require(
        metadata.get("labels", {}).get("cloud.googleapis.com/location") == REGION and ready(raw),
        "El servicio existente debe estar listo en us-central1.",
    )
    annotations = metadata.get("annotations", {})
    require(
        annotations.get("run.googleapis.com/ingress") == "all"
        and annotations.get("run.googleapis.com/invoker-iam-disabled", "false") == "false",
        "El servicio debe conservar ingress=all y la comprobación IAM de invocación.",
    )
    traffic(raw)
    template = raw["spec"]["template"]
    spec = template["spec"]
    limits = template.get("metadata", {}).get("annotations", {})
    require(
        spec.get("serviceAccountName") == f"{name}@{PROJECT}.iam.gserviceaccount.com"
        and spec.get("containerConcurrency") == 8
        and spec.get("timeoutSeconds") == 45
        and limits.get("autoscaling.knative.dev/minScale", "0") == "0"
        and limits.get("autoscaling.knative.dev/maxScale") == "1"
        and limits.get("run.googleapis.com/cpu-throttling", "true") == "true",
        "Los límites o la identidad runtime no coinciden con el plan existente.",
    )
    containers = spec.get("containers", [])
    require(len(containers) == 1, "Se exige exactamente un contenedor por servicio.")
    container = containers[0]
    resources = container.get("resources", {}).get("limits", {})
    require(
        resources.get("cpu") in {"1", "1000m"} and resources.get("memory") in {"1Gi", "1024Mi"},
        "Se exige 1 CPU y 1 GiB por contenedor.",
    )
    entries = container.get("env", [])
    env = {item["name"]: item.get("value") for item in entries}
    require(len(env) == len(entries), "Variables runtime duplicadas.")
    url = raw.get("status", {}).get("url", "")
    require(
        re.fullmatch(r"https://[a-z0-9-]+\.[a-z0-9-]+\.run\.app", url)
        and env.get("PUBLIC_AGENT_URL") == url,
        "PUBLIC_AGENT_URL debe coincidir con la URL HTTPS canónica del servicio existente.",
    )
    expected = {
        "MODEL_MODE": "vertex",
        "VERTEX_PROJECT_ID": PROJECT,
        "VERTEX_LOCATION": "global",
        "MODEL_ID": "gemini-3.1-flash-lite",
    }
    require(
        not any(key in env for key in ("GOOGLE_API_KEY", "GEMINI_API_KEY", "RESEND_API_KEY")),
        "El runtime debe usar ADC; no se admiten claves de API en estas revisiones.",
    )
    if name == "itinerary-agent":
        expected.update(PEER_USE_IAM="true", EMAIL_AGENT_URL=email_url, ENABLE_MCP="false")
        require(
            all(env.get(key) == value for key, value in expected.items()),
            "Runtime de itinerarios incompatible.",
        )
        return []
    expected.update(
        EMAIL_TRANSPORT="gmail",
        EMAIL_SENDER="student@example.com",
        ENABLE_REAL_EMAIL="true",
        GMAIL_LEDGER="gcs",
        GMAIL_LEDGER_BUCKET=BUCKET,
        GMAIL_CREDENTIALS_FILE="/secrets/gmail/authorized-user.json",
    )
    require(
        all(env.get(key) == value for key, value in expected.items()),
        "Runtime de Gmail personal/GCS incompatible.",
    )
    approval = next((item for item in entries if item["name"] == "APPROVAL_SIGNING_KEY"), {})
    require("value" not in approval, "La clave de aprobación debe proceder de Secret Manager.")
    refs = [
        secret_ref(approval.get("valueFrom", {}).get("secretKeyRef", {}), "email-agent-approval")
    ]
    mounts = [
        item
        for item in container.get("volumeMounts", [])
        if item.get("mountPath") == "/secrets/gmail"
    ]
    require(len(mounts) == 1, "Falta el montaje de credenciales Gmail protegido.")
    volumes = [item for item in spec.get("volumes", []) if item.get("name") == mounts[0]["name"]]
    require(len(volumes) == 1, "Volumen Gmail no válido.")
    secret = volumes[0].get("secret", {})
    items = secret.get("items", [])
    require(
        len(items) == 1 and items[0].get("path") == "authorized-user.json",
        "Archivo Gmail no válido.",
    )
    refs.append(
        secret_ref(
            {"name": secret.get("secretName"), "key": items[0].get("key")}, "example-gmail-oauth"
        )
    )
    return refs


def runtime_config(raw):
    value = copy.deepcopy(raw["spec"]["template"])
    value.setdefault("metadata", {}).pop("name", None)
    for key in ("run.googleapis.com/client-name", "run.googleapis.com/client-version"):
        value["metadata"].get("annotations", {}).pop(key, None)
    for container in value["spec"]["containers"]:
        container.pop("image", None)
    return value, {
        key: item
        for key, item in raw["metadata"].get("annotations", {}).items()
        if key
        not in {
            "run.googleapis.com/client-name",
            "run.googleapis.com/client-version",
            "run.googleapis.com/operation-id",
            "serving.knative.dev/creator",
            "serving.knative.dev/lastModifier",
        }
    }


def run_command(args):
    try:
        result = subprocess.run(args, check=True, capture_output=True, text=True, timeout=1200)
        return json.loads(result.stdout) if "--format=json" in args else None
    except (subprocess.SubprocessError, OSError, json.JSONDecodeError) as exc:
        # Las salidas del proveedor pueden contener configuración o material sensible.
        raise ReleaseError(f"Falló {args[0]}: se detuvo la preparación sin reintentar.") from exc


def cloud_args(*args):
    return ["gcloud", *args, "--project", PROJECT, "--quiet", "--format=json"]


def service_args(name):
    return cloud_args("run", "services", "describe", name, "--region", REGION)


def private_policy(policy, required_member=None, role=None):
    bindings = policy.get("bindings", [])
    require(
        not any(
            member in {"allUsers", "allAuthenticatedUsers"}
            for binding in bindings
            for member in binding.get("members", [])
        ),
        "Se detectó acceso público; este workflow no cambia IAM.",
    )
    if required_member:
        require(
            any(
                binding.get("role") == role
                and required_member in binding.get("members", [])
                and not binding.get("condition")
                for binding in bindings
            ),
            "Falta un permiso runtime explícito requerido; configure IAM fuera del workflow.",
        )


def preflight(run):
    services = {name: run(service_args(name)) for name in SERVICES}
    refs = validate_service(services["email-agent"], "email-agent")
    validate_service(
        services["itinerary-agent"],
        "itinerary-agent",
        services["email-agent"]["status"]["url"],
    )
    private_policy(run(cloud_args("projects", "get-iam-policy", PROJECT)))
    for name in SERVICES:
        policy = run(cloud_args("run", "services", "get-iam-policy", name, "--region", REGION))
        private_policy(
            policy,
            f"serviceAccount:itinerary-agent@{PROJECT}.iam.gserviceaccount.com"
            if name == "email-agent"
            else None,
            "roles/run.invoker",
        )
    repo = run(
        cloud_args("artifacts", "repositories", "describe", REPOSITORY, "--location", REGION)
    )
    require(repo.get("format") == "DOCKER", "Falta el repositorio Docker existente.")
    bucket = run(cloud_args("storage", "buckets", "describe", f"gs://{BUCKET}"))
    require(
        bucket.get("uniform_bucket_level_access") is True
        and bucket.get("public_access_prevention") == "enforced",
        "El bucket GCS durable debe existir con UBLA y prevención de acceso público.",
    )
    private_policy(
        run(cloud_args("storage", "buckets", "get-iam-policy", f"gs://{BUCKET}")),
        f"serviceAccount:email-agent@{PROJECT}.iam.gserviceaccount.com",
        "roles/storage.objectUser",
    )
    for name, version in refs:
        metadata = run(cloud_args("secrets", "versions", "describe", version, "--secret", name))
        require(
            metadata.get("state") == "ENABLED",
            "Falta una versión habilitada de los secretos Gmail.",
        )
    return services


def stage_runtime(source, destination):
    source = Path(source).absolute()
    destination = Path(destination).absolute()
    require(
        not destination.exists() and not destination.is_relative_to(source),
        "El staging debe ser nuevo y externo.",
    )
    paths = [source / "pyproject.toml", source / "uv.lock"]
    for package in ("common", "email_agent", "tourism_agent"):
        paths.extend(sorted((source / package).glob("*.py")))
    paths.extend(source / package / "Dockerfile" for package in ("email_agent", "tourism_agent"))
    require(
        all(
            any(path.parent.name == package for path in paths)
            for package in ("common", "email_agent", "tourism_agent")
        ),
        "Falta código runtime.",
    )
    for path in paths:
        require(
            path.is_file() and path.resolve().is_relative_to(source.resolve()),
            "Archivo runtime ausente o fuera de scope.",
        )
        for item in (path, *path.parents):
            require(
                not item.is_symlink() and not getattr(item.stat(), "st_file_attributes", 0) & 0x400,
                "No se permiten symlinks ni reparse points en staging.",
            )
    destination.mkdir(parents=True)
    for path in paths:
        target = destination / path.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)


def update_command(name, image):
    require(
        name in SERVICES
        and re.fullmatch(
            re.escape(IMAGE_ROOT + "/" + SERVICES[name]) + r"@sha256:[0-9a-f]{64}", image
        ),
        "Solo se permiten los dos servicios aprobados con una imagen del registro por digest.",
    )
    return cloud_args(
        "run", "services", "update", name, "--region", REGION, "--image", image, "--no-traffic"
    )


def execute(source, stage, env, run=run_command):
    tag = validate_request(env)
    before = preflight(run)
    stage_runtime(source, stage)
    run(["gcloud", "auth", "configure-docker", f"{REGION}-docker.pkg.dev", "--quiet"])
    images = {}
    for name, package in SERVICES.items():
        tagged = f"{IMAGE_ROOT}/{package}:{tag}"
        dockerfile = "email_agent" if package == "email" else "tourism_agent"
        run(
            [
                "docker",
                "build",
                "--file",
                str(Path(stage) / dockerfile / "Dockerfile"),
                "--tag",
                tagged,
                str(stage),
            ]
        )
        run(["docker", "push", tagged])
        metadata = run(cloud_args("artifacts", "docker", "images", "describe", tagged))
        digest = metadata.get("image_summary", {}).get("digest", "")
        require(
            re.fullmatch(r"sha256:[0-9a-f]{64}", digest),
            "El registro no devolvió un digest válido.",
        )
        images[name] = f"{IMAGE_ROOT}/{package}@{digest}"
    # Revalidar antes de la primera escritura detecta cambios concurrentes de configuración.
    current = preflight(run)
    require(
        all(
            runtime_config(before[name]) == runtime_config(current[name])
            and traffic(before[name]) == traffic(current[name])
            for name in SERVICES
        ),
        "El runtime cambió durante el build; no se despliega.",
    )
    result = {}
    for name in SERVICES:
        run(update_command(name, images[name]))
        after = run(service_args(name))
        require(
            runtime_config(before[name]) == runtime_config(after)
            and traffic(before[name]) == traffic(after),
            "La configuración o el tráfico cambió; "
            "se detiene sin promoción ni rollback automático.",
        )
        revision = after.get("status", {}).get("latestCreatedRevisionName", "")
        require(
            re.fullmatch(re.escape(name) + r"-[a-z0-9-]+", revision),
            "No se pudo identificar la nueva revisión.",
        )
        raw = run(cloud_args("run", "revisions", "describe", revision, "--region", REGION))
        require(
            ready(raw) and raw.get("status", {}).get("imageDigest") == images[name],
            "La revisión no está lista con el digest esperado.",
        )
        result[name] = {"revision": revision, "image": images[name], "traffic": "sin promoción"}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["validate-inputs", "release"])
    parser.add_argument("--stage", type=Path)
    args = parser.parse_args()
    try:
        validate_request(os.environ)
        if args.command == "release":
            require(args.stage is not None, "Indique un staging externo mediante --stage.")
            print(json.dumps(execute(Path.cwd(), args.stage, os.environ), ensure_ascii=False))
        else:
            print("Identidad y destino validados; no se contactó a GCP.")
    except (ReleaseError, KeyError, TypeError, IndexError) as exc:
        detail = str(exc) if isinstance(exc, ReleaseError) else "configuración no reconocida"
        parser.exit(1, f"Preparación detenida: {detail}\n")


if __name__ == "__main__":
    main()
