"""
VeteranDesk Master CLI Runner.

Usage:
  python main.py api        - Launch FastAPI backend (port 8000)
  python main.py dashboard  - Launch Streamlit dashboard (port 8501)
  python main.py test       - Run full pytest test suite with coverage
"""

import sys
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


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "api"
    if cmd == "api":
        run_api()
    elif cmd == "dashboard":
        run_dashboard()
    elif cmd == "test":
        run_tests()
    else:
        print(f"Unknown command: {cmd}")
        print("Valid commands: api | dashboard | test")
