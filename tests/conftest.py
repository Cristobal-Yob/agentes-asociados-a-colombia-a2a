import os
import tempfile
from pathlib import Path


def pytest_configure(config):
    if not config.option.basetemp:
        work_dir = Path(os.environ.get("COLOMBIA_WORK_DIR") or tempfile.gettempdir()).resolve()
        if work_dir.is_relative_to(Path(__file__).resolve().parents[1]):
            raise ValueError("El directorio temporal de pruebas debe estar fuera del proyecto")
        scratch = work_dir / "colombia-pytest"
        scratch.mkdir(parents=True, exist_ok=True)
        config.option.basetemp = tempfile.mkdtemp(prefix="pytest-", dir=scratch)
