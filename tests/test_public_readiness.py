"""Publication defaults are offline and do not borrow the owner's credentials."""

import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def example_values(path):
    return dict(
        line.split("=", 1)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    )


def test_reference_environment_requires_explicit_live_opt_in():
    values = example_values(ROOT / ".env.example")
    assert values["MODEL_MODE"] == "fixture"
    assert values["EMAIL_TRANSPORT"] == "mock"
    assert values["ENABLE_REAL_EMAIL"] == "false"
    assert values["ENABLE_MCP"] == "false"
    assert values["VERTEX_PROJECT_ID"] == "YOUR_VERTEX_PROJECT_ID"
    assert "example-project" not in (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "@gmail.com" not in (ROOT / ".env.example").read_text(encoding="utf-8")
    assert not any("../../work" in value for value in values.values())


def test_compose_defaults_are_loopback_offline_and_do_not_mount_owner_grants():
    services = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))["services"]
    assert set(services) == {"tourism", "email", "agenda"}
    for service in services.values():
        assert all(port.startswith("127.0.0.1:") for port in service["ports"])
        assert service["environment"]["MODEL_MODE"] == "${MODEL_MODE:-fixture}"
        assert all("oauth" not in str(mount).lower() for mount in service.get("volumes", []))
    email = services["email"]["environment"]
    assert email["EMAIL_TRANSPORT"] == "${EMAIL_TRANSPORT:-mock}"
    assert email["ENABLE_REAL_EMAIL"] == "${ENABLE_REAL_EMAIL:-false}"


def test_ui_template_does_not_select_owners_cloud_project():
    values = example_values(ROOT / "demo_ui/.env.example")
    assert values["COLOMBIA_UI_FIRESTORE_PROJECT"] == "YOUR_FIRESTORE_PROJECT_ID"
    assert not values["COLOMBIA_UI_GOOGLE_CLIENT_SECRET"]
    assert not values["COLOMBIA_UI_ITINERARY_KEY"]
    assert not values["COLOMBIA_UI_EMAIL_KEY"]


def test_basic_trip_example_uses_typed_no_send_path():
    request = json.loads((ROOT / "examples/tourism.json").read_text(encoding="utf-8"))
    assert request.get("message", "") == ""
    assert request["context"]["city"] == "Bogotá"
    assert request["context"]["days"] == 2
    assert request["draft_email"] is False
    assert not request.get("recipient")


def test_publication_guidance_does_not_claim_a_license_or_clean_history():
    security = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
    release = (ROOT / "docs/public-release.md").read_text(encoding="utf-8")
    assert "private" in security.lower()
    assert "license" in release.lower() and "pending" in release.lower()
    assert "history" in release.lower() and "Actions" in release
    assert "visibility" in release.lower()
    assert not (ROOT / "LICENSE").exists()


def test_student_learning_path_does_not_grant_hosted_access_or_distribute_credentials():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    release = (ROOT / "docs/public-release.md").read_text(encoding="utf-8")
    assert "## Student learning path" in readme
    assert "Educational source prepared for publication" in readme
    assert "does not grant students access" in readme
    assert "institutional credentials" in readme
    assert "Student-facing" in release
    assert "Client-facing" not in release
