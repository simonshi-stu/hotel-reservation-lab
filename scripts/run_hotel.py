"""One launch entry point, two explicit models; no silent schema conversion."""
import argparse
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def launch_spec(model, port):
    if model not in ("room", "type"):
        raise ValueError("model must be room or type")
    if not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    cwd = ROOT if model == "room" else ROOT / "labs" / "sqlite_inventory"
    module = "hotel_demo.server" if model == "room" else "hotel.demo"
    return cwd, [sys.executable, "-B", "-m", module, "--port", str(port)]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model", choices=("room", "type"), help="room: concrete rooms + extra beds; type: bounded relocation policy lab")
    parser.add_argument("--port", type=int, default=8019)
    args = parser.parse_args()
    cwd, command = launch_spec(args.model, args.port)
    temp = cwd / ".runtime" / "tmp"
    temp.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update(TEMP=str(temp), TMP=str(temp), TMPDIR=str(temp), PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1")
    suffix = "" if args.model == "room" else "planning"
    print(f"Model={args.model}; independent database; http://127.0.0.1:{args.port}/{suffix}", flush=True)
    return subprocess.call(command, cwd=cwd, env=env)


if __name__ == "__main__":
    raise SystemExit(main())
