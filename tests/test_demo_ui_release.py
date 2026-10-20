"""Release preparation is offline and must not expand source or cloud scope."""

import re
from pathlib import Path

import pytest

from demo_ui.config import Config
from demo_ui.release import BUILD_FILES, RUNTIME_FILES, stage, validate

ROOT = Path(__file__).resolve().parents[1]


def release_environment():
    return {
        "GITHUB_REPOSITORY": "Cristobal-Yob/agentes-asociados-a-colombia-a2a",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_SHA": "a" * 40,
        "UI_APPROVED_SHA": "a" * 40,
        "UI_GCP_PROJECT": "example-project",
        "UI_APPROVED_PROJECT": "example-project",
        "UI_APPROVED_OWNER_EMAIL": "owner@example.com",
        "UI_GCP_REGION": "us-central1",
        "UI_SERVICE": "classroom-ui",
        "UI_ALLOWED_EMAIL": "owner@example.com",
        "UI_PYTHON_BASE_IMAGE": "python:3.12-slim@sha256:" + "a" * 64,
        "UI_ORIGIN": "https://verified-colombia.example",
        "UI_ITINERARY_URL": "https://itinerary.example.invalid",
        "UI_EMAIL_URL": "https://email.example.invalid",
        "UI_HUB_RETURN": "https://hub.example.invalid/classroom",
        "UI_SENDER_EMAIL": "sender@example.com",
        "UI_FIRESTORE_DATABASE": "classroom-ui",
        "UI_IMAGE_REPOSITORY": (
            "us-central1-docker.pkg.dev/example-project/colombia-ui/classroom-ui"
        ),
        "UI_RUNTIME_SERVICE_ACCOUNT": "ui-runtime@example-project.iam.gserviceaccount.com",
        "UI_DEPLOY_SERVICE_ACCOUNT": "ui-deploy@example-project.iam.gserviceaccount.com",
        "UI_WIF_PROVIDER": (
            "projects/123/locations/global/workloadIdentityPools/github/providers/ui"
        ),
        "UI_OAUTH_ID_SECRET": "ui-oauth-id:1",
        "UI_OAUTH_SECRET": "ui-oauth:1",
        "UI_ITINERARY_KEY_SECRET": "itinerary-agent-api-key:1",
        "UI_EMAIL_KEY_SECRET": "email-agent-api-key:1",
    }


def test_stage_exact_inventory_outside_authoring_checkout(tmp_path):
    target = tmp_path / "stage"
    inventory = stage(ROOT, target)
    assert set(inventory) == set(RUNTIME_FILES + BUILD_FILES + (".dockerignore",))
    actual = {p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file()}
    assert actual == set(inventory)
    assert all(len(value) == 64 for value in inventory.values())
    assert not (target / "common").exists()
    assert not (target / ".git").exists()
    assert not (target / "demo_ui/.env.example").exists()
    assert not (target / "demo_ui/requirements-test.txt").exists()
    with pytest.raises(ValueError):
        stage(ROOT, target)
    with pytest.raises(ValueError):
        stage(ROOT, ROOT / "forbidden-stage")


def test_release_requires_exact_scope_and_immutable_inputs():
    environment = release_environment()
    validate(environment)
    for field, value in (
        ("GITHUB_REF", "refs/heads/feature"),
        ("UI_APPROVED_SHA", "b" * 40),
        ("UI_GCP_PROJECT", "other-project"),
        ("UI_ALLOWED_EMAIL", "student@example.com"),
        ("UI_PYTHON_BASE_IMAGE", "python:3.12-slim"),
        ("UI_ORIGIN", "https://site.example,INJECTED=value"),
        ("UI_FIRESTORE_DATABASE", "colombia-a2a-email"),
        ("UI_OAUTH_SECRET", "ui-oauth:latest"),
        ("UI_SERVICE", "email-agent"),
    ):
        with pytest.raises(ValueError, match="required|match|separate|destination|origin"):
            validate({**environment, field: value})


def test_workflow_is_separate_manual_protected_and_no_traffic():
    cd = (ROOT / ".github/workflows/cd-demo-ui.yml").read_text()
    assert "workflow_dispatch:" in cd and "push:" not in cd
    assert "environment: classroom-ui-release" in cd
    assert "--no-traffic" in cd and '--image="$UI_IMAGE_DIGEST"' in cd
    assert "--allow-unauthenticated" not in cd and "--no-invoker-iam-check" not in cd
    assert "services update-traffic" not in cd
    assert "gcloud run services describe" in cd
    assert "create_credentials_file: true" in cd
    assert "actions/checkout@d23441" in cd


def test_ci_installs_only_pinned_ui_dependencies_in_an_isolated_environment():
    ci = (ROOT / ".github/workflows/ci-demo-ui.yml").read_text()
    assert "uv sync" not in ci and "uv run" not in ci
    assert "uv venv --python 3.12 .venv-ui" in ci
    assert "uv pip install --python .venv-ui/bin/python --no-deps" in ci
    assert "-r demo_ui/requirements.txt -r demo_ui/requirements-test.txt" in ci
    assert "uv pip check --python .venv-ui/bin/python" in ci
    assert "PYTEST_DISABLE_PLUGIN_AUTOLOAD: '1'" in ci
    assert "-p pytest_asyncio.plugin -c /dev/null --noconftest" in ci
    assert "tests/test_demo_ui.py tests/test_demo_ui_release.py -q" in ci
    assert "ruff check --isolated" in ci and "ruff format --isolated" in ci
    requirements = (ROOT / "demo_ui/requirements-test.txt").read_text().splitlines()
    pins = [line for line in requirements if line and not line.startswith("#")]
    assert all(re.fullmatch(r"[A-Za-z0-9_-]+==[0-9]+(?:\.[0-9]+)+", line) for line in pins)
    assert {line.split("==")[0] for line in pins} == {
        "colorama",
        "iniconfig",
        "packaging",
        "pluggy",
        "pygments",
        "pytest",
        "pytest-asyncio",
        "ruff",
    }


def test_configuration_rejects_origin_injection_and_shared_state():
    common = dict(
        client_id="test.apps.googleusercontent.com",
        client_secret="test",
        project="example-project",
        database="colombia-ui",
        itinerary_key="i" * 32,
        email_key="e" * 32,
        owner_email="owner@example.com",
        itinerary_url="https://itinerary.example.invalid",
        email_url="https://email.example.invalid",
        hub_return="https://hub.example.invalid/classroom",
        sender_email="sender@example.com",
    )
    assert Config(origin="http://127.0.0.1:8110", **common).ready
    for origin in (
        "https://foo.example,INJECTED=value",
        "https://foo.example\n",
        "http://example.com",
        "https://user:pass@example.com",
        "https://example.com/path",
    ):
        assert not Config(origin=origin, **common).ready
    assert not Config(
        origin="https://example.com", **{**common, "database": "colombia-a2a-email"}
    ).ready
