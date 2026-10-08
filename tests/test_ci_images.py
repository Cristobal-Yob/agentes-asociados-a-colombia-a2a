"""Container admission contracts use fake processes and never run Docker or cloud."""

import importlib.util
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("ci_images", ROOT / "scripts/ci_images.py")
images = importlib.util.module_from_spec(spec)
spec.loader.exec_module(images)
SHA = "a" * 40


def test_verify_builds_every_image_before_isolated_runtime_checks():
    calls = []

    def execute(args, **kwargs):
        calls.append(args)
        if args[:2] == ["git", "archive"]:
            return source_archive(images)
        return SHA if args == ["git", "rev-parse", "HEAD"] else ""

    images.verify(SHA, execute=execute)
    assert len(images.IMAGES) == 2
    commands = calls[3:]
    assert len(commands) >= 2 * 3
    for name in images.IMAGES:
        build, *checks = images.commands(name, SHA)
        assert build in commands and build[:2] == ["docker", "build"]
        assert "--platform=linux/amd64" in build and name + "-ci:" + SHA in build
        for command in checks:
            assert command in commands and commands.index(command) > commands.index(build)
            assert (
                "--network=none" in command
                and "--read-only" in command
                and "--cap-drop=ALL" in command
            )
        assert "google.auth.default = denied" in checks[-1][-1]
    assert not any("push" in command or "login" in command for command in calls)


@pytest.mark.parametrize(
    "head,status,sha", [("b" * 40, "", SHA), (SHA, " M source.py", SHA), (SHA, "", "main; command")]
)
def test_non_exact_or_dirty_source_never_reaches_docker(head, status, sha):
    calls = []

    def execute(args, **kwargs):
        calls.append(args)
        return head if args == ["git", "rev-parse", "HEAD"] else status

    with pytest.raises(ValueError):
        images.verify(sha, execute=execute)
    assert not any(command[0] == "docker" for command in calls)


def test_safe_argv_and_bounded_failure_hide_child_output():
    def failed(args, **kwargs):
        assert kwargs["shell"] is False and kwargs["timeout"] == 1200
        assert kwargs["capture_output"] is True
        return subprocess.CompletedProcess(
            args, 1, "synthetic-sensitive-stdout", "synthetic-sensitive-stderr"
        )

    with patch.object(images.subprocess, "run", failed), pytest.raises(RuntimeError) as failure:
        images.run(["docker", "build", "literal argument"])
    assert "sensitive" not in str(failure.value)


def test_ci_image_checks_have_no_cloud_credentials():
    workflow = yaml.load(
        (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"), Loader=yaml.BaseLoader
    )
    job = workflow["jobs"]["images"]
    assert job.get("permissions", workflow.get("permissions")) == {"contents": "read"}
    assert any("scripts/ci_images.py --sha" in step.get("run", "") for step in job["steps"])
    assert not any(
        "auth@" in step.get("uses", "") or "secrets." in str(step) for step in job["steps"]
    )


def test_isolated_publication_branch_triggers_ci_without_widening_main():
    from fnmatch import fnmatchcase

    import yaml

    workflow = yaml.load(
        (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"), Loader=yaml.BaseLoader
    )
    branches = workflow["on"]["push"]["branches"]
    assert any(fnmatchcase("main", pattern) for pattern in branches)
    assert any(fnmatchcase("codex/langgraph-ci", pattern) for pattern in branches)
    assert not any(fnmatchcase("release/other", pattern) for pattern in branches)


def test_ignored_cache_cannot_enter_the_committed_docker_context(tmp_path, monkeypatch):
    import io
    import tarfile

    ci_images = images

    ignored = tmp_path / ".ruff_cache"
    ignored.mkdir()
    (ignored / "unversioned").write_text("synthetic ignored bytes", encoding="utf-8")
    monkeypatch.setattr(ci_images, "ROOT", tmp_path)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for path in {config[0] for config in ci_images.IMAGES.values()} | {"committed.txt"}:
            content = b"committed source"
            entry = tarfile.TarInfo(path)
            entry.size = len(content)
            archive.addfile(entry, io.BytesIO(content))
    built = []

    def execute(args, **kwargs):
        if args == ["git", "rev-parse", "HEAD"]:
            return SHA
        if args[:2] == ["git", "archive"]:
            assert args == ["git", "archive", "--format=tar", SHA]
            assert kwargs["binary"] is True
            return buffer.getvalue()
        if args[:2] == ["docker", "build"]:
            context = Path(kwargs.get("cwd", ci_images.ROOT))
            assert not (context / ".ruff_cache").exists()
            assert (context / "committed.txt").read_text(encoding="utf-8") == "committed source"
            built.append(context)
        return ""

    ci_images.verify(SHA, execute=execute)
    assert built and all(not directory.exists() for directory in built)
    assert (ignored / "unversioned").exists()


def source_archive(module):
    import io
    import tarfile

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for path in {config[0] for config in module.IMAGES.values()}:
            content = b"synthetic committed Dockerfile"
            entry = tarfile.TarInfo(path)
            entry.size = len(content)
            archive.addfile(entry, io.BytesIO(content))
    return buffer.getvalue()


@pytest.mark.parametrize(
    "entry_name,linked", [("../escape", False), ("/absolute", False), ("linked", True)]
)
def test_committed_archive_rejects_paths_and_links_before_any_build(entry_name, linked):
    import io
    import tarfile

    ci_images = images

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        entry = tarfile.TarInfo(entry_name)
        if linked:
            entry.type = tarfile.SYMTYPE
            entry.linkname = "../escape"
        archive.addfile(entry)
    calls = []

    def execute(args, **kwargs):
        calls.append(args)
        return buffer.getvalue()

    with pytest.raises(ValueError), ci_images.committed_source(SHA, execute):
        pytest.fail("Unsafe source must never reach a build")
    assert len(calls) == 1 and calls[0][:2] == ["git", "archive"]
