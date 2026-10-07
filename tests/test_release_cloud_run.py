import copy
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import release_cloud_run as release


def request_env():
    return {
        "EXPECTED_GITHUB_REPOSITORY": "example/two-agents",
        "GITHUB_REPOSITORY": "example/two-agents",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_SHA": "a" * 40,
        "GITHUB_RUN_ID": "123",
        "GITHUB_RUN_ATTEMPT": "1",
        "GCP_WORKLOAD_IDENTITY_PROVIDER": (
            "projects/123456/locations/global/workloadIdentityPools/github/providers/actions"
        ),
        "GCP_DEPLOY_SERVICE_ACCOUNT": "release@example-project.iam.gserviceaccount.com",
    }


def service(name="email-agent"):
    email_url = "https://service.example.invalid"
    own_url = email_url if name == "email-agent" else "https://service.example.invalid"
    values = {
        "MODEL_MODE": "vertex",
        "VERTEX_PROJECT_ID": "example-project",
        "VERTEX_LOCATION": "global",
        "MODEL_ID": "gemini-3.1-flash-lite",
        "PUBLIC_AGENT_URL": own_url,
        "ENABLE_MCP": "false",
    }
    if name == "email-agent":
        values.update(
            EMAIL_TRANSPORT="gmail",
            EMAIL_SENDER="student@example.com",
            ENABLE_REAL_EMAIL="true",
            GMAIL_LEDGER="gcs",
            GMAIL_LEDGER_BUCKET="example-project-email-agent-ledger",
            GMAIL_CREDENTIALS_FILE="/secrets/gmail/authorized-user.json",
        )
    else:
        values.update(PEER_USE_IAM="true", EMAIL_AGENT_URL=email_url)
    container = {
        "image": "example.invalid/old@sha256:" + "b" * 64,
        "resources": {"limits": {"cpu": "1", "memory": "1Gi"}},
        "env": [{"name": key, "value": value} for key, value in values.items()],
    }
    spec = {
        "serviceAccountName": f"{name}@example-project.iam.gserviceaccount.com",
        "containerConcurrency": 8,
        "timeoutSeconds": 45,
        "containers": [container],
    }
    if name == "email-agent":
        container["env"].append(
            {
                "name": "APPROVAL_SIGNING_KEY",
                "valueFrom": {"secretKeyRef": {"name": "email-agent-approval", "key": "2"}},
            }
        )
        container["volumeMounts"] = [{"name": "gmail", "mountPath": "/secrets/gmail"}]
        spec["volumes"] = [
            {
                "name": "gmail",
                "secret": {
                    "secretName": "example-gmail-oauth",
                    "items": [{"key": "3", "path": "authorized-user.json"}],
                },
            }
        ]
    return {
        "metadata": {
            "name": name,
            "labels": {"cloud.googleapis.com/location": "us-central1"},
            "annotations": {"run.googleapis.com/ingress": "all"},
        },
        "spec": {
            "template": {
                "metadata": {
                    "annotations": {
                        "autoscaling.knative.dev/minScale": "0",
                        "autoscaling.knative.dev/maxScale": "1",
                        "run.googleapis.com/cpu-throttling": "true",
                    }
                },
                "spec": spec,
            },
            "traffic": [{"revisionName": name + "-00001-old", "percent": 100}],
        },
        "status": {"url": own_url, "conditions": [{"type": "Ready", "status": "True"}]},
    }


def test_missing_or_foreign_identity_never_reaches_cloud(tmp_path):
    calls = []
    for key, bad in [
        ("GITHUB_REF", "refs/heads/other"),
        ("GITHUB_REPOSITORY", "attacker/other"),
        ("GCP_DEPLOY_SERVICE_ACCOUNT", "release@example-project.iam.gserviceaccount.com"),
        ("GCP_WORKLOAD_IDENTITY_PROVIDER", ""),
    ]:
        env = request_env()
        env[key] = bad
        with pytest.raises(release.ReleaseError):
            release.execute(tmp_path, tmp_path / "stage", env, run=lambda args: calls.append(args))
    assert calls == []


def test_preflight_failure_never_builds_pushes_or_deploys(tmp_path):
    calls = []

    def run(args):
        calls.append(args)
        raise release.ReleaseError("Recurso no disponible")

    with pytest.raises(release.ReleaseError):
        release.execute(tmp_path, tmp_path / "stage", request_env(), run=run)
    assert len(calls) == 1
    assert calls[0][:4] == ["gcloud", "run", "services", "describe"]


def test_valid_existing_services_keep_the_fixed_account_and_durable_gcs():
    mail = service()
    assert release.validate_service(mail, "email-agent") == [
        ("email-agent-approval", "2"),
        ("example-gmail-oauth", "3"),
    ]
    assert (
        release.validate_service(
            service("itinerary-agent"), "itinerary-agent", mail["status"]["url"]
        )
        == []
    )


def test_canonical_cloud_run_url_with_project_number_is_supported():
    raw = service()
    raw["status"]["url"] = "https://service.example.invalid"
    for entry in raw["spec"]["template"]["spec"]["containers"][0]["env"]:
        if entry["name"] == "PUBLIC_AGENT_URL":
            entry["value"] = raw["status"]["url"]
    assert release.validate_service(raw, "email-agent")


@pytest.mark.parametrize(
    ("key", "bad"),
    [
        ("GMAIL_LEDGER", "sqlite"),
        ("EMAIL_SENDER", "other@example.com"),
        ("VERTEX_PROJECT_ID", "other-project"),
        ("PUBLIC_AGENT_URL", "https://pending.invalid"),
        ("GMAIL_CREDENTIALS_FILE", "/tmp/grant.json"),
    ],
)
def test_incompatible_existing_runtime_is_rejected(key, bad):
    raw = service()
    for entry in raw["spec"]["template"]["spec"]["containers"][0]["env"]:
        if entry["name"] == key:
            entry["value"] = bad
    with pytest.raises(release.ReleaseError):
        release.validate_service(raw, "email-agent")


def test_secret_latest_and_implicit_latest_traffic_are_rejected():
    raw = service()
    raw["spec"]["template"]["spec"]["volumes"][0]["secret"]["items"][0]["key"] = "latest"
    with pytest.raises(release.ReleaseError):
        release.validate_service(raw, "email-agent")
    raw = service()
    raw["spec"]["traffic"] = [{"latestRevision": True, "percent": 100}]
    with pytest.raises(release.ReleaseError):
        release.validate_service(raw, "email-agent")


def test_runtime_change_is_detected_but_only_image_and_cli_metadata_may_change():
    before = service()
    after = copy.deepcopy(before)
    after["spec"]["template"]["spec"]["containers"][0]["image"] = "new@sha256:" + "c" * 64
    after["spec"]["template"]["metadata"]["name"] = "new-revision"
    assert release.runtime_config(before) == release.runtime_config(after)
    after["spec"]["template"]["spec"]["timeoutSeconds"] = 300
    assert release.runtime_config(before) != release.runtime_config(after)


def test_stage_contains_only_runtime_allowlist_and_rejects_escaping_symlinks(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    for name in ("pyproject.toml", "uv.lock"):
        (source / name).write_text("fixture")
    for name in ("common", "email_agent", "tourism_agent"):
        (source / name).mkdir()
        (source / name / "__init__.py").write_text("")
        if name != "common":
            (source / name / "Dockerfile").write_text("FROM scratch")
    (source / "gha-creds-test.json").write_text("private fixture")
    (source / ".env").write_text("private fixture")
    stage = tmp_path / "stage"
    release.stage_runtime(source, stage)
    assert not (stage / ".env").exists()
    assert not (stage / "gha-creds-test.json").exists()
    assert (stage / "email_agent" / "Dockerfile").is_file()
    link = source / "common" / "escape.py"
    try:
        link.symlink_to(Path(__file__))
    except OSError:
        pytest.skip("La cuenta local no permite crear enlaces simbólicos")
    with pytest.raises(release.ReleaseError):
        release.stage_runtime(source, tmp_path / "other-stage")


def test_only_exact_digest_update_without_traffic_is_permitted():
    image = release.IMAGE_ROOT + "/email@sha256:" + "d" * 64
    args = release.update_command("email-agent", image)
    assert "--no-traffic" in args
    assert args[args.index("--image") + 1] == image
    assert "--set-env-vars" not in args
    assert "--service-account" not in args
    for name, bad_image in [
        ("observatory", image),
        ("email-agent", release.IMAGE_ROOT + "/email:latest"),
        ("email-agent", image.replace("example-project", "other")),
    ]:
        with pytest.raises(release.ReleaseError):
            release.update_command(name, bad_image)


def test_reparse_point_runtime_file_is_rejected_without_copy(tmp_path, monkeypatch):
    source = Path(__file__).resolve().parents[1]
    original = Path.stat

    def stat(path, *args, **kwargs):
        value = original(path, *args, **kwargs)
        if path.name == "config.py":
            return SimpleNamespace(st_mode=value.st_mode, st_file_attributes=0x400)
        return value

    monkeypatch.setattr(Path, "stat", stat)
    stage = tmp_path / "stage"
    with pytest.raises(release.ReleaseError):
        release.stage_runtime(source, stage)
    assert not stage.exists()


@pytest.mark.parametrize("wrong_digest", [False, True])
def test_two_existing_services_release_by_digest_or_stop_without_retry(tmp_path, wrong_digest):
    services = {name: service(name) for name in release.SERVICES}
    calls = []
    images = {}

    def run(args):
        calls.append(args)
        if args[:4] == ["gcloud", "run", "services", "describe"]:
            return copy.deepcopy(services[args[4]])
        if args[:4] == ["gcloud", "run", "services", "update"]:
            name = args[4]
            image = args[args.index("--image") + 1]
            images[name] = image
            services[name]["spec"]["template"]["spec"]["containers"][0]["image"] = image
            services[name]["status"]["latestCreatedRevisionName"] = name + "-00002-new"
            return {}
        if args[:4] == ["gcloud", "run", "revisions", "describe"]:
            name = next(name for name in services if args[4].startswith(name + "-"))
            return {
                "status": {
                    "conditions": [{"type": "Ready", "status": "True"}],
                    "imageDigest": "incorrect" if wrong_digest else images[name],
                }
            }
        if "get-iam-policy" in args:
            return {
                "bindings": [
                    {
                        "role": "roles/run.invoker",
                        "members": [
                            "serviceAccount:itinerary-agent@example-project.iam.gserviceaccount.com"
                        ],
                    },
                    {
                        "role": "roles/storage.objectUser",
                        "members": [
                            "serviceAccount:email-agent@example-project.iam.gserviceaccount.com"
                        ],
                    },
                ]
            }
        if args[:4] == ["gcloud", "artifacts", "repositories", "describe"]:
            return {"format": "DOCKER"}
        if args[:4] == ["gcloud", "storage", "buckets", "describe"]:
            return {"uniform_bucket_level_access": True, "public_access_prevention": "enforced"}
        if args[:4] == ["gcloud", "secrets", "versions", "describe"]:
            return {"state": "ENABLED"}
        if args[:5] == ["gcloud", "artifacts", "docker", "images", "describe"]:
            return {"image_summary": {"digest": "sha256:" + "d" * 64}}
        assert args[0] == "docker" or args[:3] == ["gcloud", "auth", "configure-docker"]
        return None

    source = Path(__file__).resolve().parents[1]
    if wrong_digest:
        with pytest.raises(release.ReleaseError, match="digest"):
            release.execute(source, tmp_path / "stage", request_env(), run=run)
    else:
        result = release.execute(source, tmp_path / "stage", request_env(), run=run)
        assert set(result) == set(release.SERVICES)
        assert all(value["traffic"] == "sin promoción" for value in result.values())
    updates = [args for args in calls if args[:4] == ["gcloud", "run", "services", "update"]]
    assert len(updates) == (1 if wrong_digest else 2)
    assert all("--no-traffic" in args for args in updates)
    assert all("@sha256:" in args[args.index("--image") + 1] for args in updates)
