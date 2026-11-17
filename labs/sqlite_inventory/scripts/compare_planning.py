"""Reproduce synthetic room planning comparison without touching baseline databases."""
import argparse,json
from datetime import datetime,timezone
from pathlib import Path
from hotel.planning import compare
ROOT=Path(__file__).resolve().parents[1]
def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--seed",type=int,default=61104);p.add_argument("--scale",type=int,choices=(1,2),default=1)
    args=p.parse_args()
    stamp=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    folder=ROOT/".runtime"/("room-planning-"+stamp)
    result=compare(folder,args.seed,args.scale)
    summary={"synthetic":True,"seed":args.seed,"request_count":result["request_count"],"input_sha256":result["input_sha256"],
             "data_directory":str(folder),"metrics":{k:{n:v for n,v in r.items() if n not in ("bookings","inventory","moves","rejected")} for k,r in result["strategies"].items()}}
    target=ROOT/"reports"/("room-planning-"+stamp+".json")
    target.write_text(json.dumps(summary,indent=2),encoding="utf-8")
    print(json.dumps(summary,indent=2));print(str(target))
if __name__=="__main__":main()

