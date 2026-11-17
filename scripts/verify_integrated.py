"""Run both independent hotel models without mixing imports or database schemas."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
LAB = ROOT / "labs" / "sqlite_inventory"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiments", action="store_true", help="Also reproduce the 712-request type-level study and room-level invariant audit")
    args = parser.parse_args()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    output = ROOT / "results" / ("integration-" + stamp)
    output.mkdir(parents=True, exist_ok=False)
    temp = output / "tmp"
    temp.mkdir()
    env = os.environ.copy()
    env.update(TEMP=str(temp), TMP=str(temp), TMPDIR=str(temp), PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1")
    # Separate subprocesses avoid the two legacy 'tests' and 'scripts' packages colliding.
    jobs = [("room_level_tests", ROOT, ["-m", "unittest", "discover", "-s", "tests", "-v"]),
            ("type_level_tests", LAB, ["-m", "unittest", "discover", "-s", "tests", "-v"])]
    if args.experiments:
        (LAB / ".runtime").mkdir(exist_ok=True)
        jobs += [("type_level_frontier", LAB, ["-m", "scripts.policy_frontier"]),
                 ("room_level_invariants", ROOT, ["scripts/audit_invariants.py"])]
    results = []
    for name, cwd, command in jobs:
        result = subprocess.run([sys.executable, "-B", *command], cwd=cwd, env=env,
                                capture_output=True, text=True, encoding="utf-8", errors="replace")
        (output / (name + ".log")).write_text(result.stdout + result.stderr, encoding="utf-8")
        results.append({"check": name, "returncode": result.returncode, "passed": result.returncode == 0})
        print(name, "PASS" if result.returncode == 0 else "FAIL", flush=True)
    report = {"checks": results, "synthetic_only": True, "shared_database": False,
              "note": "Integration of source, entry points and validation; not a merged allocator or schema."}
    (output / "summary.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(output)
    return 0 if all(row["passed"] for row in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
