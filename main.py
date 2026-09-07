"""
VeteranDesk Master CLI Runner.

Usage:
  python main.py api        - Launch FastAPI backend (port 8000)
  python main.py dashboard  - Launch Streamlit dashboard (port 8501)
  python main.py test       - Run full pytest test suite with coverage
"""

import sys
from pathlib import Path

# Ensure project root is on sys.path
_project_root = Path(__file__).resolve().parent
while _project_root.parent != _project_root and not (_project_root / "veterandesk").is_dir():
    _project_root = _project_root.parent
if (_project_root / "veterandesk").is_dir() and str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

import subprocess
import uvicorn

from veterandesk.config import settings
from veterandesk.logging import get_logger

logger = get_logger("veterandesk.runner")


def run_api() -> None:
    logger.info("starting_fastapi_server", port=8000)
    uvicorn.run("veterandesk.api.app:app", host="0.0.0.0", port=8000, reload=True)


def run_dashboard() -> None:
    logger.info("starting_streamlit_dashboard", port=8501)
    subprocess.run([
        sys.executable, "-m", "streamlit", "run",
        "veterandesk/dashboard/Home.py",
        "--server.port=8501",
        "--server.headless=true"
    ])


def run_tests() -> None:
    subprocess.run([
        sys.executable, "-m", "pytest", "tests/",
        "-v", "--cov=veterandesk", "--cov-report=term-missing"
    ])


def run_migrate() -> None:
    from veterandesk.database.migration import run_migration
    logger.info("running_database_schema_migration")
    run_migration()


if __name__ == "__main__":
    import streamlit as st
    from pathlib import Path

    if st.runtime.exists():
        _home_path = Path(__file__).resolve().parent / "veterandesk" / "dashboard" / "Home.py"
        with open(_home_path, encoding="utf-8") as _f:
            exec(compile(_f.read(), str(_home_path), "exec"), globals())
    else:
        cmd = sys.argv[1] if len(sys.argv) > 1 else "api"
        if cmd == "api":
            run_api()
        elif cmd == "dashboard":
            run_dashboard()
        elif cmd == "test":
            run_tests()
        elif cmd == "migrate":
            run_migrate()
        else:
            print(f"Unknown command: {cmd}")
            print("Valid commands: api | dashboard | test | migrate")

