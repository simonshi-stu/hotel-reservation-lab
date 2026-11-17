"""Paired synthetic policies, fixed seeds, no forecast/hindsight optimisation."""
import json
from datetime import datetime,timezone
from pathlib import Path
from hotel.planning import compare
ROOT=Path(__file__).resolve().parents[1]
def main():
    stamp=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    folder=ROOT/".runtime"/("policy-frontier-"+stamp);folder.mkdir()
    rows=[]
    for seed in (61104,71,202,999):
        result=compare(folder/("seed-"+str(seed)),seed,1)
        metrics={k:{n:r[n] for n in ("accepted","people","room_nights","revenue_cents","rejection_rate","violations","cohorts")} for k,r in result["strategies"].items()}
        base=metrics["no_upgrade"]
        for key,r in metrics.items():
            r["delta_vs_no_upgrade"]={n:r[n]-base[n] for n in ("accepted","people","room_nights","revenue_cents")}
        rows.append({"seed":seed,"input_sha256":result["input_sha256"],"requests":result["request_count"],"metrics":metrics})
        print("Finished seed "+str(seed),flush=True)
    totals={}
    for policy in rows[0]["metrics"]:
        totals[policy]={n:sum(row["metrics"][policy][n] for row in rows) for n in ("accepted","people","room_nights","revenue_cents")}
        totals[policy]["requests"]=sum(row["requests"] for row in rows)
        totals[policy]["rejection_rate"]=1-totals[policy]["accepted"]/totals[policy]["requests"]
        totals[policy]["violations"]=sum(len(row["metrics"][policy]["violations"]) for row in rows)
    report={"synthetic":True,"paired_cases":rows,"pooled_totals":totals,"data_directory":str(folder),
            "comparison":"Same calendar, demand, order and price within each seed; no future demand available to any policy.",
            "limitations":["Four constructed seeds, not a population estimate or significance test.",
                           "Cohort acceptance differences reflect observed outcomes, not causal discrimination estimates.",
                           "Reserve of one premium room per type/night is heuristic, not a revenue guarantee.",
                           "New v2 experiment schema includes initial_assigned; old experiment databases are not migrated."]}
    target=ROOT/"reports"/("policy-frontier-"+stamp+".json")
    target.write_text(json.dumps(report,indent=2),encoding="utf-8")
    print(json.dumps(totals,indent=2));print(str(target))
if __name__=="__main__":main()

