import os
from pathlib import Path


def pytest_configure(config):
    if not config.option.basetemp:
        config.option.basetemp = str(
            Path(__file__).resolve().parents[3] / "work" / f"pytest-{os.getpid()}"
        )
