import os
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def safe_env():
    env = os.environ.copy()
    for key in list(env):
        if any(
            word in key.upper()
            for word in [
                "API_KEY",
                "TOKEN",
                "CREDENTIAL",
                "APPROVAL_SIGNING_KEY",
                "LANGSMITH",
                "LANGCHAIN_TRACING",
            ]
        ):
            env.pop(key, None)
    env.update(
        {
            "MODEL_MODE": "fixture",
            "EMAIL_TRANSPORT": "mock",
            "ENABLE_REAL_EMAIL": "false",
            "ENABLE_MCP": "false",
            "PEER_USE_IAM": "false",
            "LANGSMITH_TRACING": "false",
            "LANGCHAIN_TRACING_V2": "false",
            "HOST": "127.0.0.1",
        }
    )
    return env


class OwnedServices:
    """Start/stop only owned child processes; never kill a port or unrelated PID."""

    def __init__(self):
        self.tour_port = free_port()
        self.email_port = free_port()
        while self.email_port == self.tour_port:
            self.email_port = free_port()
        self.tour_url = f"http://127.0.0.1:{self.tour_port}"
        self.email_url = f"http://127.0.0.1:{self.email_port}"
        self.keys = {role: secrets.token_urlsafe(32) for role in ("tourism", "email")}
        self.children = []
        self.logs = []
        self.work = Path(
            os.getenv("COLOMBIA_WORK_DIR", str(ROOT.parent.parent / "work" / "colombia-runtime"))
        )
        self.work.mkdir(parents=True, exist_ok=True)

    def start(self, agent):
        port = self.tour_port if agent == "tourism" else self.email_port
        url = self.tour_url if agent == "tourism" else self.email_url
        env = safe_env()
        env.update(
            {
                "PORT": str(port),
                "PUBLIC_AGENT_URL": url,
                "EMAIL_AGENT_URL": self.email_url,
                "AGENT_API_KEY": self.keys[agent],
                "EMAIL_AGENT_API_KEY": self.keys["email"] if agent == "tourism" else "",
                "WORKFLOW_DB_PATH": str(self.work / f"{agent}-{port}.sqlite"),
            }
        )
        log_path = self.work / f"{agent}-{port}.log"
        stream = log_path.open("w", encoding="utf-8")
        process = subprocess.Popen(
            [sys.executable, "-m", f"{agent}_agent"],
            cwd=ROOT,
            env=env,
            stdout=stream,
            stderr=stream,
        )
        self.children.append((process, stream))
        self.logs.append(log_path)
        deadline = time.monotonic() + 30
        with httpx.Client(timeout=0.5) as client:
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError(f"El proceso propio {agent} terminó durante el inicio")
                try:
                    if client.get(url + "/health").status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                time.sleep(0.1)
        raise RuntimeError(f"El proceso propio {agent} superó el tiempo máximo de inicio")

    def close(self):
        for process, stream in reversed(self.children):
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            stream.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
