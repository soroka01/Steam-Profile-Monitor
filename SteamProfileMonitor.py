import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def ensure_project_venv() -> None:
    venv_python = BASE_DIR / ".venv" / "Scripts" / "python.exe"
    if not venv_python.exists():
        return

    current_python = Path(sys.executable).resolve()
    if current_python == venv_python.resolve():
        return

    os.execv(str(venv_python), [str(venv_python), str(Path(__file__).resolve()), *sys.argv[1:]])


if __name__ == "__main__":
    ensure_project_venv()

    import asyncio

    from steam_monitor_app.bot import main

    asyncio.run(main())
