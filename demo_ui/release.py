"""Offline allowlisted packaging and fail-closed release-input checks."""

import argparse
import hashlib
import json
import os
import re
import shutil
from pathlib import Path

from demo_ui.config import valid_origin

RUNTIME_FILES = (
    "demo_ui/__init__.py",
    "demo_ui/__main__.py",
    "demo_ui/app.py",
    "demo_ui/agents.py",
    "demo_ui/auth.py",
    "demo_ui/config.py",
    "demo_ui/store.py",
    "demo_ui/requirements.txt",
    "demo_ui/static/index.html",
    "demo_ui/static/app.js",
    "demo_ui/static/styles.css",
)
BUILD_FILES = ("demo_ui/Dockerfile", "demo_ui/Dockerfile.dockerignore")


def stage(root, destination):
    root, destination = Path(root).resolve(), Path(destination).resolve()
    if destination == root or destination.is_relative_to(root) or destination.exists():
        raise ValueError("Use a new staging directory outside the checkout")
    inventory = {}
    checked = []
    for relative in RUNTIME_FILES + BUILD_FILES:
        path = root / relative
        if any(part.is_symlink() or part.is_junction() for part in (path, *path.parents)):
            raise ValueError("Symlinks and junctions are not release inputs")
        if not path.is_file() or not path.resolve().is_relative_to(root):
            raise ValueError("Missing or escaping release input")
        checked.append((relative, path))
    destination.mkdir(parents=True)
    for relative, path in checked:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        inventory[relative] = hashlib.sha256(target.read_bytes()).hexdigest()
    # The stage contains no source-control history, tests, OAuth files or agent credentials.
    (destination / ".dockerignore").write_text("**\n!demo_ui/\n!demo_ui/**\n", encoding="utf-8")
    inventory[".dockerignore"] = hashlib.sha256(
        (destination / ".dockerignore").read_bytes()
    ).hexdigest()
    return inventory


def validate(environment):
    expected = {
        "GITHUB_REPOSITORY": "Cristobal-Yob/agentes-asociados-a-colombia-a2a",
        "GITHUB_REF": "refs/heads/main",
        "UI_GCP_PROJECT": environment.get("UI_APPROVED_PROJECT", ""),
        "UI_GCP_REGION": "us-central1",
        "UI_SERVICE": "classroom-ui",
        "UI_ALLOWED_EMAIL": environment.get("UI_APPROVED_OWNER_EMAIL", ""),
    }
    if any(environment.get(key) != value for key, value in expected.items()):
        raise ValueError("Repository, ref, identity or target does not match the approved scope")
    project = environment.get("UI_GCP_PROJECT", "")
    if not re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]", project):
        raise ValueError("An explicit approved project is required")
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", environment.get("UI_ALLOWED_EMAIL", "")):
        raise ValueError("An explicit approved owner email is required")
    sha = environment.get("GITHUB_SHA", "")
    if not re.fullmatch(r"[a-f0-9]{40}", sha) or environment.get("UI_APPROVED_SHA") != sha:
        raise ValueError("An explicitly selected source SHA is required")
    base = environment.get("UI_PYTHON_BASE_IMAGE", "")
    if not re.fullmatch(r"(?:docker.io/library/)?python:3\.12[\w.-]*@sha256:[a-f0-9]{64}", base):
        raise ValueError("A reviewed Python 3.12 base image digest is required")
    origin = environment.get("UI_ORIGIN", "")
    if not valid_origin(origin):
        raise ValueError("The verified own HTTPS origin is required")
    for field in ("UI_ITINERARY_URL", "UI_EMAIL_URL"):
        if not valid_origin(environment.get(field, "")):
            raise ValueError("Explicit approved agent origins are required")
    from urllib.parse import urlsplit

    hub = urlsplit(environment.get("UI_HUB_RETURN", ""))
    if not valid_origin(hub.scheme + "://" + hub.netloc) or hub.query:
        raise ValueError("A fixed approved hub return is required")
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", environment.get("UI_SENDER_EMAIL", "")):
        raise ValueError("An explicit approved sender email is required")
    database = environment.get("UI_FIRESTORE_DATABASE", "")
    if not re.fullmatch(r"[a-z][a-z0-9-]{3,62}", database) or database in {
        "colombia-a2a-itinerary",
        "colombia-a2a-email",
    }:
        raise ValueError("A separate UI Firestore database is required")
    service_account = environment.get("UI_RUNTIME_SERVICE_ACCOUNT", "")
    deployer = environment.get("UI_DEPLOY_SERVICE_ACCOUNT", "")
    for value in (service_account, deployer):
        if not re.fullmatch(
            rf"[a-z][a-z0-9-]+@{re.escape(project)}\.iam\.gserviceaccount\.com", value
        ):
            raise ValueError("Explicit dedicated project service accounts are required")
    if service_account == deployer:
        raise ValueError("Runtime and deploy identities must remain separate")
    if not re.fullmatch(
        r"projects/\d+/locations/global/workloadIdentityPools/[\w-]+/providers/[\w-]+",
        environment.get("UI_WIF_PROVIDER", ""),
    ):
        raise ValueError("A repository-restricted WIF provider is required")
    registry = environment.get("UI_IMAGE_REPOSITORY", "")
    if not re.fullmatch(
        rf"us-central1-docker\.pkg\.dev/{re.escape(project)}/[a-z0-9-]+/classroom-ui", registry
    ):
        raise ValueError("An existing dedicated image destination is required")
    for key in (
        "UI_OAUTH_ID_SECRET",
        "UI_OAUTH_SECRET",
        "UI_ITINERARY_KEY_SECRET",
        "UI_EMAIL_KEY_SECRET",
    ):
        if not re.fullmatch(r"[a-z][a-z0-9-]+:[1-9]\d*", environment.get(key, "")):
            raise ValueError("Explicit numeric Secret Manager version references are required")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("validate", "stage"))
    parser.add_argument("--destination")
    args = parser.parse_args()
    if args.command == "validate":
        validate(os.environ)
        print("Release scope validated; no cloud operation performed.")
    else:
        if not args.destination:
            parser.error("--destination is required")
        print(json.dumps(stage(Path(__file__).resolve().parents[1], args.destination), indent=2))
