import pytest

from scripts.cloud_plan import plan


def test_cloud_plan_requires_explicit_project_and_has_two_private_services():
    commands = plan("clases-505520", "deployer@example.com", "lesson-test")
    assert all("--project" in command and "--account" in command for command in commands)
    deploys = [command for command in commands if command[1:3] == ["run", "deploy"]]
    assert [command[3] for command in deploys] == ["colombia-email", "colombia-itinerary"]
    assert all(
        "--no-allow-unauthenticated" in command and "--cpu-throttling" in command
        for command in deploys
    )
    assert not any("config" in command and "set" in command for command in commands)
    build = next(command for command in commands if command[1:3] == ["builds", "submit"])
    assert build[build.index("--service-account") + 1] == (
        "projects/clases-505520/serviceAccounts/"
        "colombia-build@clases-505520.iam.gserviceaccount.com"
    )
    assert build[build.index("--region") + 1] == "us-central1"
    assert all("MODEL_MODE=vertex" in " ".join(command) for command in deploys)
    assert all("VERTEX_PROJECT_ID=clases-505520" in " ".join(command) for command in deploys)
    assert all("VERTEX_LOCATION=global" in " ".join(command) for command in deploys)
    assert "aiplatform.googleapis.com" in commands[0]
    roles = [command for command in commands if "roles/aiplatform.user" in command]
    assert len(roles) == 2
    assert all("GOOGLE_API_KEY" not in " ".join(command) for command in commands)
    email = deploys[0]
    assert "EMAIL_TRANSPORT=gmail" in " ".join(email)
    assert "EMAIL_SENDER=cristo.68.23@gmail.com" in " ".join(email)
    assert "GMAIL_LEDGER=gcs" in " ".join(email) and "--set-secrets" in email
    ledger = [
        command for command in commands if "gs://clases-505520-colombia-email-ledger" in command
    ]
    assert any("--public-access-prevention" in command for command in ledger)
    assert any("--lifecycle-file" in command for command in ledger)
    iam = [command for command in ledger if "add-iam-policy-binding" in command]
    assert len(iam) == 1 and "roles/storage.objectUser" in iam[0]
    assert "serviceAccount:colombia-email@clases-505520.iam.gserviceaccount.com" in iam[0]


def test_plan_cannot_target_an_unrelated_ambient_project():
    with pytest.raises(ValueError, match="clases-505520"):
        plan("unrelated-project", "deployer@example.com", "lesson-test")
