"""The bench jobs' gate: every metric benchmarks/limits.json names for a runtime
and host must stay under its ceiling (ms, or MiB for snapshot-size). Metrics
not named there are charted but not gated. benchmarks/gate.sh runs it.

    python benchmarks/check_ceilings.py RUNTIME HOST [--plan FILE] [--again FILE...]

Checks bench-results.json, the first run. Exits 0 if every ceiling is met and
1 if one is not. A timing over its ceiling may be another job's load on a
shared runner rather than a regression: if timings alone are over, and
--plan is given, it writes there the `<mode> <workload>...` lines to measure
them again with, and exits 3. With --again, the results of that second run,
it judges those timings by them alone.
"""

import argparse
import json
import os
import sys
from pathlib import Path


root = Path(__file__).resolve().parent.parent

# The mode of `just bench` that measures each timing ("<metric>/<workload>",
# in ms). A size (snapshot-size, in MiB) is not measured again: load does not
# change it, so a breach fails at once.
MODES = {
    "cold": "cold",
    "cold-snap": "cold-snap",
    "warm-restore": "warm-restore",
    "restore-cost": "warm-restore",
    "warm-stateful": "warm-stateful",
    "parallel-exec": "parallel",
}


def describe(result, ceilings):
    unit = result["unit"]
    return f"{result['name']}: {result['value']} {unit} is over the ceiling of {ceilings[result['name']]} {unit}"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("runtime")
    parser.add_argument("host")
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--again", type=Path, nargs="+")
    args = parser.parse_args()

    limits = json.loads((root / "benchmarks" / "limits.json").read_text())
    ceilings = limits.get(args.runtime, {}).get(args.host, {})
    if not ceilings:
        print(f"::notice::no ceilings for {args.runtime} on {args.host} in benchmarks/limits.json: charted, not gated")
        return 0
    first = json.loads((root / "bench-results.json").read_text())
    # A renamed workload, or a mode that printed no median, leaves a ceiling
    # with nothing to check: said, as it would otherwise pass unseen.
    measured = {r["name"] for r in first}
    for name in sorted(set(ceilings) - measured):
        print(f"::warning::{name} has a ceiling in benchmarks/limits.json but no result")
    over = [r for r in first if r["name"] in ceilings and r["value"] > ceilings[r["name"]]]
    if not over:
        print(f"all {len(set(ceilings) & measured)} ceilings with a result met")
        return 0
    again = [r for r in over if r["unit"] == "ms" and r["name"].split("/")[0] in MODES]

    # A size, or a timing with no mode here, fails as it is: so does the job,
    # whatever a second run would say of the others.
    if len(again) < len(over) or not (args.plan or args.again):
        for r in over:
            unknown = r["unit"] == "ms" and r not in again
            print(
                f"::error::{describe(r, ceilings)}"
                + (", with no mode in check_ceilings.py to measure it again" if unknown else "")
            )
        return 1

    if args.again:
        second, failed = {}, []
        for path in args.again:
            try:
                second.update((r["name"], r) for r in json.loads(path.read_text()))
            except (OSError, ValueError) as error:
                failed.append(f"the second measurement's results ({path.name}): {error}")
        # The job summary's table is the first run's: these decide the gate.
        rows = ["| metric | ceiling | first run | second run |", "|---|---|---|---|"]
        for r in again:
            value = second.get(r["name"], {}).get("value")
            rows.append(f"| {r['name']} | {ceilings[r['name']]} | {r['value']} | {'-' if value is None else value} |")
            if value is None:
                failed.append(f"{r['name']}: not measured again")
            elif value > ceilings[r["name"]]:
                failed.append(f"{describe(second[r['name']], ceilings)} again")
            else:
                print(f"{r['name']}: {value} {r['unit']} on a second measurement")
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as summary:
                summary.write("\n### Timings over a ceiling, measured again (ms)\n\n" + "\n".join(rows) + "\n")
        for line in failed:
            print(f"::error::{line}")
        return 1 if failed else 0

    workloads = {}
    for r in again:
        metric, workload = r["name"].split("/", 1)
        workloads.setdefault(MODES[metric], set()).add(workload)
    # \n, not Windows' \r\n: Git Bash's read would keep the \r.
    lines = "".join(f"{mode} {' '.join(sorted(names))}\n" for mode, names in sorted(workloads.items()))
    with open(args.plan, "w", newline="\n") as plan:
        plan.write(lines)
    for r in again:
        print(f"::warning::{describe(r, ceilings)}; measuring it again")
    return 3


if __name__ == "__main__":
    sys.exit(main())
