"""Regenerate the delivered data and verify every CSV SHA-256, preserving originals."""
import json
from datetime import datetime, timezone
from pathlib import Path
from scripts.seed import generate, ROOT

def main():
    stamp=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    reference=json.loads((ROOT/"data"/"synthetic-v1"/"manifest.json").read_text(encoding="utf-8"))
    folder=ROOT/".runtime"/("reproduce-"+stamp)
    folder.mkdir(parents=True)
    generated=generate(folder/"hotel.db",folder/"csv",reference["seed"],reference["customers"],
                       reference["booking_attempts"],reference["days"])
    checks={name:digest==generated["csv_sha256"].get(name)
            for name,digest in reference["csv_sha256"].items()}
    report={"synthetic":True,"utc":stamp,"csv_hash_match":checks,
            "manifest_equal":reference==generated,"passed":all(checks.values()) and reference==generated}
    target=ROOT/"reports"/("reproduction-"+stamp+".json")
    target.write_text(json.dumps(report,indent=2),encoding="utf-8")
    print(json.dumps(report,indent=2))
    print(str(target))
    raise SystemExit(0 if report["passed"] else 1)

if __name__=="__main__":
    main()

