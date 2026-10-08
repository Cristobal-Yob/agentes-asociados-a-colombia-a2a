"""Verify exact-source classroom images without cloud authentication or publication."""

import argparse
import io
import re
import subprocess
import tarfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
IMAGES = {
    "tourism": (
        "tourism_agent/Dockerfile",
        ".",
        "/app/.venv/bin/python",
        ["common.entry", "tourism_agent.app"],
    ),
    "email": (
        "email_agent/Dockerfile",
        ".",
        "/app/.venv/bin/python",
        ["common.entry", "email_agent.app"],
    ),
}
IMPORT_CHECK = """import os, socket, subprocess, importlib
# Windows system roots are required by stdlib SSL; retain no credential settings.
system_paths = {}
for key in ('SystemRoot', 'WINDIR'):
    if key in os.environ: system_paths[key] = os.environ[key]
os.environ.clear()
os.environ.update(system_paths)
def denied(*args, **kwargs):
    raise RuntimeError('CI denies network, ADC and child calls')
socket.socket.connect = denied
socket.socket.connect_ex = denied
subprocess.Popen._execute_child = denied
import google.auth
google.auth.default = denied
import httpx
httpx.HTTPTransport.handle_request = denied
httpx.AsyncHTTPTransport.handle_async_request = denied
import langchain_core, langgraph
for module in MODULES: importlib.import_module(module)
print('Runtime imports passed; network, ADC and child calls denied')
"""
EXTRA_CHECK = """
from common.config import Settings
agent=MODULES[-1].split('.')[0]
settings=Settings.offline(approval_secret='synthetic-ci-signing-key-32-bytes-long')
app=importlib.import_module(agent+'.app').create_app(settings)
assert app.routes and app.state.service.graph
import uvicorn
startup=[]
uvicorn.run=lambda *args,**kwargs: startup.append((args,kwargs))
importlib.import_module(agent+'.__main__')
assert startup[0][0][0]==agent+'.app:create_app' and startup[0][1]['factory'] is True
print('Entrypoint factory constructed offline; no inference or mail')
"""


def failure_summary(args, stderr):
    """Expose bounded container diagnostics, never the command or captured stdout."""
    image = next(
        (arg for arg in args if re.fullmatch(r"(?:tourism|email)-ci:[a-f0-9]{40}", arg)),
        "unidentified",
    )
    stage = (
        "build"
        if args[:2] == ["docker", "build"]
        else "dependency-check"
        if "--entrypoint" in args and args[args.index("--entrypoint") + 1] == "uv"
        else "runtime-import"
        if args[:2] == ["docker", "run"]
        else "source-check"
    )
    detail = (
        stderr.decode("utf-8", errors="replace") if isinstance(stderr, bytes) else (stderr or "")
    )
    detail = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", detail[:8192])
    detail = re.sub(r"(?i)\bBearer\s+\S+", "Bearer [redacted]", detail)
    detail = re.sub(
        r"(?i)\b(authorization|api[_-]?key|token|secret|password|credential)\b\s*[:=]\s*[^\s,;]+",
        r"\1=[redacted]",
        detail,
    )
    detail = re.sub(r"https?://[^\s]+", "[url redacted]", detail)
    detail = re.sub(r"\b(?:gh[pousr]_|github_pat_|ya29\.)[A-Za-z0-9_.-]+", "[redacted]", detail)
    detail = re.sub(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "[redacted]", detail)
    detail = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", detail)
    return f"Container {stage} failed for {image}: {detail.strip()[:1024] or 'no stderr'}"


def run(args, *, timeout=1200, binary=False, cwd=None):
    result = subprocess.run(
        args,
        cwd=ROOT if cwd is None else cwd,
        text=not binary,
        capture_output=True,
        shell=False,
        timeout=timeout,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(failure_summary(args, result.stderr))
    return result.stdout


def commands(name, sha):
    if name not in IMAGES or not re.fullmatch(r"[a-f0-9]{40}", sha):
        raise ValueError("Allowed image and exact SHA required")
    dockerfile, context, python, modules = IMAGES[name]
    tag = name + "-ci:" + sha
    sandbox = [
        "docker",
        "run",
        "--rm",
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
    ]
    check = sandbox + ["--entrypoint", python, tag]
    dependency = sandbox + [
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,nodev,size=16m,mode=1777",
        "--entrypoint",
        "uv",
        tag,
        "pip",
        "check",
        "--python",
        python,
        "--offline",
        "--no-cache",
    ]
    return [
        [
            "docker",
            "build",
            "--platform=linux/amd64",
            "--label",
            "org.opencontainers.image.revision=" + sha,
            "--tag",
            tag,
            "--file",
            dockerfile,
            context,
        ],
        dependency,
        check + ["-B", "-c", "MODULES=" + repr(modules) + "\n" + IMPORT_CHECK + EXTRA_CHECK],
    ]


@contextmanager
def committed_source(sha, execute):
    raw = execute(["git", "archive", "--format=tar", sha], binary=True)
    if not isinstance(raw, bytes) or len(raw) > 64 * 1024 * 1024:
        raise ValueError("Bounded committed source archive required")
    with TemporaryDirectory(prefix="ci-committed-source-") as temporary:
        directory = Path(temporary)
        try:
            with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
                members = archive.getmembers()
                if (
                    len(members) > 10000
                    or sum(member.size for member in members) > 64 * 1024 * 1024
                ):
                    raise ValueError("Committed source inventory exceeds limits")
                for member in members:
                    path = PurePosixPath(member.name)
                    if (
                        path.is_absolute()
                        or ".." in path.parts
                        or "\\" in member.name
                        or not (member.isfile() or member.isdir())
                    ):
                        raise ValueError("Nonportable or linked archive entry denied")
                archive.extractall(directory, filter="data")
        except tarfile.TarError:
            raise ValueError("Invalid committed source archive") from None
        yield directory


def verify(sha, *, execute=run):
    if not re.fullmatch(r"[a-f0-9]{40}", sha):
        raise ValueError("Exact commit SHA required")
    if (
        execute(["git", "rev-parse", "HEAD"]).strip() != sha
        or execute(["git", "status", "--porcelain", "--untracked-files=all"]).strip()
    ):
        raise ValueError("Only clean exact checked-out source can be built")
    with committed_source(sha, execute) as source:
        for name in IMAGES:
            for command in commands(name, sha):
                execute(command, cwd=source)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--sha", required=True)
    verify(parser.parse_args().sha)
