"""python -m hotel_sim --input data/week.json --output results/week.json"""
import argparse
import json
from pathlib import Path

from .scenario import load_scenario, simulate


def main():
    parser = argparse.ArgumentParser(description="Run the independent synthetic hotel allocation candidate")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    data, digest = load_scenario(args.input)
    result = simulate(data, digest)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({mode: report["summary"] for mode, report in result["policies"].items()}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
