"""Start one local component with an explicitly selected backend configuration.

This helper never discovers .env files, prints credentials, initializes or
migrates a database. Start API, worker and web in separate terminals.
"""

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("component", choices=("api", "worker", "web"))
    parser.add_argument("--config", type=Path)
    parser.add_argument("--port", type=int)
    args = parser.parse_args()
    values = dict(os.environ)
    if args.config:
        if not args.config.is_file():
            parser.error("explicit configuration file not found")
        from dotenv import dotenv_values

        values.update({k: v for k, v in dotenv_values(args.config).items() if k.startswith("SI_") and v is not None})
    if args.component in {"api", "worker"} and not values.get("SI_DATABASE_URL"):
        parser.error("configure SI_DATABASE_URL explicitly; no database will be created")
    if args.component == "api":
        command = [sys.executable, "-m", "uvicorn", "waku.server.app:create_app", "--factory", "--host", "127.0.0.1", "--port", str(args.port or 8016)]
        cwd = ROOT
    elif args.component == "worker":
        command, cwd = [sys.executable, "-m", "waku.workers.runner"], ROOT
    else:
        node = shutil.which("node")
        if not node:
            parser.error("Node.js is required; add its executable directory to PATH")
        command = [node, "node_modules/vite/bin/vite.js", "--host", "127.0.0.1", "--port", str(args.port or 5186), "--strictPort"]
        cwd = ROOT / "apps/web"
        # Backend-only settings and secrets never enter the frontend process.
        for key in tuple(values):
            if key.startswith(("SI_", "VITE_")):
                del values[key]
        values["SI_API_URL"] = "http://127.0.0.1:8016"
    try:
        raise SystemExit(subprocess.call(command, cwd=cwd, env=values))
    except KeyboardInterrupt:
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
