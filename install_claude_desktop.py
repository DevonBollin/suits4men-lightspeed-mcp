"""Add (or update) this server in Claude Desktop's config.

    .venv\\Scripts\\python.exe install_claude_desktop.py

Re-run it if you move the project folder. It backs up the config first,
changes only the "lightspeed" entry under mcpServers, and leaves every other
server and setting alone. No secrets go into the config: the server reads
them from Windows Credential Manager.
"""

import json
import os
import shutil
import sys
import time
from pathlib import Path

SERVER_NAME = "lightspeed"
PROJECT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.path.expandvars(r"%APPDATA%\Claude\claude_desktop_config.json"))


def main() -> int:
    python = PROJECT_DIR / ".venv" / "Scripts" / "python.exe"
    server = PROJECT_DIR / "server.py"
    if not python.exists():
        print(f"Virtual environment not found at {python}. Run 'uv sync' in the project folder first.")
        return 1

    config = {}
    if CONFIG_PATH.exists():
        config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        backup = CONFIG_PATH.with_name(f"{CONFIG_PATH.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(CONFIG_PATH, backup)
        print(f"Backed up the current config to {backup}")
    else:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)

    config.setdefault("mcpServers", {})[SERVER_NAME] = {"command": str(python), "args": [str(server)]}

    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, CONFIG_PATH)
    print(f"Added '{SERVER_NAME}' to {CONFIG_PATH}")
    print("Fully quit Claude Desktop (system tray icon > Quit) and start it again to load it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
