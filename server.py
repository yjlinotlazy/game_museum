#!/usr/bin/env python3
"""Application entry point: python3 server.py"""

import subprocess
from pathlib import Path

from app import main


if __name__ == "__main__":
    project_root = Path(__file__).resolve().parent
    frontend = subprocess.Popen(
        ["npm", "run", "dev", "--", "--host", "127.0.0.1"],
        cwd=project_root,
    )
    try:
        main()
    finally:
        frontend.terminate()
        try:
            frontend.wait(timeout=3)
        except subprocess.TimeoutExpired:
            frontend.kill()
