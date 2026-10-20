"""Imprime un plan revisable de comandos de GCP. Este módulo nunca ejecuta gcloud."""

import argparse
import re
import shlex


def plan(project, account, tag, *, sender="sender@example.com"):
    if not re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]", project):
        raise ValueError("Se exige un proyecto explicito valido")
    region = "us-central1"
    repo = "example-agents"
    common = ["--project", project, "--account", account]
    email_sa = f"email-agent@{project}.iam.gserviceaccount.com"
    tour_sa = f"itinerary-agent@{project}.iam.gserviceaccount.com"
    build_sa = f"agent-build@{project}.iam.gserviceaccount.com"
    commands = []

    def add(*args):
        commands.append(["gcloud", *args, *common])

    add(
        "services",
        "enable",
        "run.googleapis.com",
        "cloudbuild.googleapis.com",
        "artifactregistry.googleapis.com",
        "iam.googleapis.com",
        "aiplatform.googleapis.com",
        "gmail.googleapis.com",
        "secretmanager.googleapis.com",
        "storage.googleapis.com",
    )
    for name in ["email-agent", "itinerary-agent", "agent-build"]:
        add("iam", "service-accounts", "create", name, "--display-name", name)
    add(
        "artifacts",
        "repositories",
        "create",
        repo,
        "--repository-format",
        "docker",
        "--location",
        region,
    )
    add(
        "artifacts",
        "repositories",
        "add-iam-policy-binding",
        repo,
        "--location",
        region,
        "--member",
        f"serviceAccount:{build_sa}",
        "--role",
        "roles/artifactregistry.writer",
    )
    source_bucket = f"gs://{project}-colombia-source"
    add(
        "storage",
        "buckets",
        "create",
        source_bucket,
        "--location",
        region,
        "--uniform-bucket-level-access",
    )
    add(
        "storage",
        "buckets",
        "add-iam-policy-binding",
        source_bucket,
        "--member",
        f"serviceAccount:{build_sa}",
        "--role",
        "roles/storage.objectViewer",
    )
    add(
        "projects",
        "add-iam-policy-binding",
        project,
        "--member",
        f"serviceAccount:{build_sa}",
        "--role",
        "roles/logging.logWriter",
    )
    for sa in [email_sa, tour_sa, build_sa]:
        add(
            "iam",
            "service-accounts",
            "add-iam-policy-binding",
            sa,
            "--member",
            f"user:{account}",
            "--role",
            "roles/iam.serviceAccountUser",
        )
    for sa in [email_sa, tour_sa]:
        for role in ["roles/aiplatform.user", "roles/serviceusage.serviceUsageConsumer"]:
            add(
                "projects",
                "add-iam-policy-binding",
                project,
                "--member",
                f"serviceAccount:{sa}",
                "--role",
                role,
            )
    ledger_bucket = f"gs://{project}-email-agent-ledger"
    add(
        "storage",
        "buckets",
        "create",
        ledger_bucket,
        "--location",
        region,
        "--uniform-bucket-level-access",
        "--public-access-prevention",
    )
    add(
        "storage",
        "buckets",
        "update",
        ledger_bucket,
        "--lifecycle-file",
        "docs/gmail-ledger-lifecycle.json",
    )
    add(
        "storage",
        "buckets",
        "add-iam-policy-binding",
        ledger_bucket,
        "--member",
        f"serviceAccount:{email_sa}",
        "--role",
        "roles/storage.objectUser",
    )
    for name, protected_file in [
        ("example-gmail-oauth", "<PROTECTED_GMAIL_CREDENTIAL_FILE_FROM_HELPER>"),
        ("email-agent-approval", "<PROTECTED_SIGNING_KEY_FILE_FROM_HELPER>"),
    ]:
        add(
            "secrets",
            "create",
            name,
            "--replication-policy",
            "automatic",
            "--data-file",
            protected_file,
        )
        add(
            "secrets",
            "add-iam-policy-binding",
            name,
            "--member",
            f"serviceAccount:{email_sa}",
            "--role",
            "roles/secretmanager.secretAccessor",
        )
    add(
        "builds",
        "submit",
        ".",
        "--config",
        "cloudbuild.yaml",
        "--service-account",
        f"projects/{project}/serviceAccounts/{build_sa}",
        "--region",
        region,
        "--machine-type",
        "e2-standard-2",
        "--timeout",
        "1200s",
        "--gcs-source-staging-dir",
        source_bucket + "/source",
        "--substitutions",
        f"_REGION={region},_REPOSITORY={repo},_TAG={tag}",
    )
    run_flags = [
        "--region",
        region,
        "--no-allow-unauthenticated",
        "--cpu",
        "1",
        "--memory",
        "1Gi",
        "--min-instances",
        "0",
        "--max-instances",
        "1",
        "--concurrency",
        "8",
        "--timeout",
        "45s",
        "--cpu-throttling",
        "--ingress",
        "all",
    ]
    add(
        "run",
        "deploy",
        "email-agent",
        "--image",
        f"{region}-docker.pkg.dev/{project}/{repo}/email:{tag}",
        "--service-account",
        email_sa,
        "--set-env-vars",
        f"MODEL_MODE=vertex,VERTEX_PROJECT_ID={project},VERTEX_LOCATION=global,"
        f"MODEL_ID=gemini-3.1-flash-lite,EMAIL_TRANSPORT=gmail,EMAIL_SENDER={sender},"
        f"GMAIL_EXPECTED_EMAIL={sender},GMAIL_OAUTH_PROJECT={project},"
        "GMAIL_CREDENTIALS_FILE=/secrets/gmail/authorized-user.json,GMAIL_LEDGER=gcs,"
        f"GMAIL_LEDGER_BUCKET={project}-email-agent-ledger,"
        "ENABLE_REAL_EMAIL=true,PUBLIC_AGENT_URL=https://pending.invalid",
        "--set-secrets",
        "APPROVAL_SIGNING_KEY=email-agent-approval:latest,"
        "/secrets/gmail/authorized-user.json=example-gmail-oauth:latest",
        *run_flags,
    )
    add(
        "run",
        "services",
        "describe",
        "email-agent",
        "--region",
        region,
        "--format",
        "value(status.url)",
    )
    # The operator must replace the explicit placeholder with the returned email URL.
    add(
        "run",
        "services",
        "update",
        "email-agent",
        "--region",
        region,
        "--update-env-vars",
        "PUBLIC_AGENT_URL=<EMAIL_URL_FROM_DESCRIBE>",
    )
    add(
        "run",
        "services",
        "add-iam-policy-binding",
        "email-agent",
        "--region",
        region,
        "--member",
        f"serviceAccount:{tour_sa}",
        "--role",
        "roles/run.invoker",
    )
    add(
        "run",
        "deploy",
        "itinerary-agent",
        "--image",
        f"{region}-docker.pkg.dev/{project}/{repo}/tourism:{tag}",
        "--service-account",
        tour_sa,
        "--set-env-vars",
        f"MODEL_MODE=vertex,VERTEX_PROJECT_ID={project},VERTEX_LOCATION=global,"
        "MODEL_ID=gemini-3.1-flash-lite,ENABLE_MCP=false,PEER_USE_IAM=true,"
        "EMAIL_AGENT_URL=<EMAIL_URL_FROM_DESCRIBE>,PUBLIC_AGENT_URL=https://pending.invalid",
        *run_flags,
    )
    add(
        "run",
        "services",
        "describe",
        "itinerary-agent",
        "--region",
        region,
        "--format",
        "value(status.url)",
    )
    add(
        "run",
        "services",
        "update",
        "itinerary-agent",
        "--region",
        region,
        "--update-env-vars",
        "PUBLIC_AGENT_URL=<ITINERARY_URL_FROM_DESCRIBE>",
    )
    for service in ["email-agent", "itinerary-agent"]:
        add(
            "run",
            "services",
            "add-iam-policy-binding",
            service,
            "--region",
            region,
            "--member",
            f"user:{account}",
            "--role",
            "roles/run.invoker",
        )
    return commands


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--project",
        required=True,
        help="Proyecto GCP seleccionado para la clase",
    )
    parser.add_argument(
        "--account", required=True, help="Cuenta explícita de la persona autorizada para desplegar"
    )
    parser.add_argument(
        "--tag", required=True, help="Etiqueta única de imagen para esta compilación aprobada"
    )
    parser.add_argument("--sender", required=True, help="Cuenta propia autorizada de Gmail")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]", args.project):
        parser.error("Identificador de proyecto no válido")
    if not re.fullmatch(r"[^\s@]+@[^\s@]+", args.account):
        parser.error("La cuenta debe ser una dirección de correo explícita")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", args.tag):
        parser.error("Etiqueta de imagen no válida")
    print(
        "SOLO PLAN. No se ejecuta ningún comando. Verifique su destino explicito. "
        "La aprobación del nuevo despliegue está pendiente."
    )
    print(
        "Gmail sender@example.com está seleccionado. "
        "Los pasos de secretos y despliegue exigen obtener y verificar "
        "la autorización OAuth de escritorio y sus archivos protegidos; "
        "requieren aprobación independiente."
    )
    print(
        "Deténgase ante cualquier salida distinta de cero. Sustituya las URL de ejemplo "
        "solo por la salida verificada de describe."
    )
    for command in plan(args.project, args.account, args.tag, sender=args.sender):
        print(shlex.join(command))


if __name__ == "__main__":
    main()
