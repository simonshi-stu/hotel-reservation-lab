"""Persist unittest outcome and detailed text without third-party test dependencies."""
import io
import json
import platform
import sqlite3
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def main():
    timestamp=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    stream=io.StringIO()
    suite=unittest.defaultTestLoader.discover(str(ROOT/"tests"))
    result=unittest.TextTestRunner(stream=stream,verbosity=2).run(suite)
    content=stream.getvalue()
    target=ROOT/"reports"/("functional-"+timestamp)
    target.with_suffix(".txt").write_text(content,encoding="utf-8")
    report={"utc":timestamp,"engine":"SQLite","python":sys.version,"platform":platform.platform(),
            "sqlite":sqlite3.sqlite_version,"tests_run":result.testsRun,"failures":len(result.failures),
            "errors":len(result.errors),"skipped":len(result.skipped),"passed":result.wasSuccessful(),
            "postgresql_exercised":False,"detail_file":target.with_suffix(".txt").name}
    target.with_suffix(".json").write_text(json.dumps(report,indent=2),encoding="utf-8")
    print(content)
    print(json.dumps(report,indent=2))
    raise SystemExit(0 if result.wasSuccessful() else 1)

if __name__=="__main__":
    main()

