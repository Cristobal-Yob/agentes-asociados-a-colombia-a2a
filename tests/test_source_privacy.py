"""Public educational source must not select somebody else's live environment."""

import re
from pathlib import Path

import pytest

from common.config import Settings
from common.model import ModelPort
from demo_ui.config import Config

ROOT = Path(__file__).resolve().parents[1]


def test_unconfigured_runtime_is_offline_and_real_email_is_disabled(monkeypatch):
    for name in ("MODEL_MODE", "EMAIL_TRANSPORT", "ENABLE_REAL_EMAIL", "VERTEX_PROJECT_ID"):
        monkeypatch.delenv(name, raising=False)
    settings = Settings.from_env("tourism")
    assert settings.model_mode == "fixture"
    assert settings.email_transport == "mock"
    assert not settings.real_email_enabled
    assert not settings.vertex_project


def test_live_model_requires_an_explicit_project_before_credentials(monkeypatch):
    import google.auth

    def denied_credentials(**kwargs):
        raise AssertionError("Credential discovery must not run before project validation")

    monkeypatch.setattr(google.auth, "default", denied_credentials)
    monkeypatch.setenv("MODEL_MODE", "vertex")
    monkeypatch.delenv("VERTEX_PROJECT_ID", raising=False)
    with pytest.raises(ValueError, match="project|proyecto"):
        ModelPort(Settings.from_env("tourism"))


def test_ui_has_no_selected_owner_upstream_or_hub_by_default():
    config = Config()
    assert not config.ready
    assert not config.owner_email
    assert not config.itinerary_url
    assert not config.email_url
    assert not config.hub_return


def test_source_contains_only_reserved_personal_email_examples():
    matches = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or any(part.startswith(".") for part in path.relative_to(ROOT).parts):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeError:
            continue
        for address in re.findall(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})", text):
            if not (
                address.endswith((".example", ".invalid", ".test"))
                or address in {"example.com", "example.org", "example.net"}
                or address.startswith("example-project.")
                and address.endswith(".iam.gserviceaccount.com")
            ):
                matches.append(str(path.relative_to(ROOT)))
    assert not matches, "Nonreserved personal/cloud email examples remain in: " + ", ".join(
        sorted(set(matches))
    )


def test_cloud_workflows_require_separate_opt_in_and_are_not_push_deployments():
    for name in ("cd-cloud-run.yml", "cd-demo-ui.yml"):
        text = (ROOT / ".github/workflows" / name).read_text(encoding="utf-8")
        assert "workflow_dispatch:" in text and "push:" not in text
        assert "PUBLIC_SOURCE_DEPLOY_ENABLED" in text
        assert "pull_request_target" not in text


@pytest.mark.parametrize(
    "role,effect_path",
    [
        ("tourism", None),
        ("email", "GMAIL_LEDGER_PATH=/state/gmail-dispatch.sqlite"),
        ("agenda", "CALENDAR_LEDGER_PATH=/state/calendar-dispatch.sqlite"),
    ],
)
def test_nonroot_images_own_state_before_using_durable_paths(role, effect_path):
    dockerfile = (ROOT / f"{role}_agent/Dockerfile").read_text(encoding="utf-8")
    before_user, after_user = dockerfile.split("USER agent", maxsplit=1)
    assert "useradd --uid 10001" in before_user
    assert re.search(r"\bmkdir(?: -p)? /state(?:\s|$)", before_user)
    assert "chown -R agent:agent /app /state" in before_user
    assert f"WORKFLOW_DB_PATH=/state/{role}.sqlite" in after_user
    if effect_path:
        assert effect_path in after_user
