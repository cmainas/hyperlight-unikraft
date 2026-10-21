#!/usr/bin/env bash
# The bench jobs' gate, after `just bench RUNTIME`: check_ceilings.py on its
# results, and on a second run of the timings over a ceiling, if any, which
# may be another job's load on a shared runner rather than a regression.
# The second run writes elsewhere (BENCH_RESULTS), so bench-results.json, the
# history CI charts, stays the first run's.
#
#     bash benchmarks/gate.sh RUNTIME HOST
set -euo pipefail

runtime=$1 host=$2
cd "$(dirname "$0")/.."
py=$(command -v python || command -v python3) || { echo "::error::no Python on the PATH"; exit 1; }
# Under the runner's temp, which it empties after each job: the trap does
# not run when the step is killed at its timeout.
base=${RUNNER_TEMP:-${TMPDIR:-/tmp}}
if command -v cygpath >/dev/null; then base=$(cygpath -u "$base"); fi
scratch=$(mktemp -d "$base/gate.XXXXXX")
trap 'rm -rf "$scratch"' EXIT
# On Windows, as C:/...: the bench recipe there is PowerShell's, and Python
# a Windows program, and neither reads Git Bash's /tmp.
if command -v cygpath >/dev/null; then scratch=$(cygpath -m "$scratch"); fi

status=0
"$py" benchmarks/check_ceilings.py "$runtime" "$host" --plan "$scratch/plan" || status=$?
[ "$status" = 3 ] || exit "$status"

# Each workload in its mode alone, and with 10 samples where the mode takes
# a count (20 the first time), so that even the agent image's cold boots
# (about 45 s each on Windows) fit the step's timeout.
again=()
while read -r mode workloads; do
    echo "::group::$mode, again: $workloads"
    # A bench that fails writes no results, which the check below reports:
    # on to the other modes meanwhile.
    BENCH_RESULTS="$scratch/$mode.json" BENCH_WORKLOADS="$workloads" BENCH_SAMPLES=10 \
        just bench "$runtime" "$mode" </dev/null || echo "::error::just bench $runtime $mode failed"
    echo "::endgroup::"
    again+=("$scratch/$mode.json")
done <"$scratch/plan"
"$py" benchmarks/check_ceilings.py "$runtime" "$host" --again "${again[@]}"
