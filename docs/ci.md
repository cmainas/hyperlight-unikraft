# CI

CI runs in three tiers, by how much of the hosts' time a change can take. These are CI tiers, not the [guest runtime support tiers](guest-support-tiers.md).

| Tier | Hosts | Runs on | Blocks a merge |
|---|---|---|---|
| 1 | Linux x86_64, Windows x86_64 (hosted runners) | every pull request and push to `main` | yes |
| 2 | macOS arm64, Windows arm64 (self-hosted) | pull requests labelled `ci/arm64` | yes, when labelled before `ci-passed` starts |
| 3 | Linux arm64, macOS arm64, Windows arm64 (self-hosted) | nightly on `main` | no |

`ci-passed` is the one required check: it passes when every tier-1 job the pull request calls for passes, and, on a pull request labelled `ci/arm64`, when its tier-2 run passes too. The labels are read when `ci-passed` starts: after adding `ci/arm64` later, re-run it.

## Tier 1: every change

[`ci.yml`](../.github/workflows/ci.yml): lint, spell check, the kernel's build, the guest images, the Rust tests, the static Linux binaries, the demos, the integrations (smolagents, MCP, verl and ClickHouse, those last two on Linux only), CPython's conformance suite and the benchmarks, on Linux and Windows. It also builds and runs the unit tests on hosted arm64 runners (macOS, Linux, Windows), which can boot no guest.

A pull request runs what its files call for:

| The pull request changes | Jobs |
|---|---|
| documentation alone: markdown, `docs/`, `site/` (not `templates/`, which `hluk` embeds) | spell check |
| `integrations/<name>/` alone, with documentation or not | lint, the Rust tests, and that integration's jobs (smolagents brings MCP's, which runs its guest through it) |
| anything else | everything |

The `ci/integrations` label adds every integration's jobs to any pull request. It is read when the `changes` job starts: after adding it, use Re-run all jobs (re-running the failed ones keeps the `changes` job's result). A push to `main` runs everything.

## Tier 2: on request

[`arm64.yml`](../.github/workflows/arm64.yml) on macOS (Hypervisor.framework) and Windows arm64 (WHP): the Rust tests, the demos, the smolagents and MCP integrations, the conformance suite and the benchmarks, with the guest images built on a hosted arm64 runner.

To request it, a maintainer labels the pull request `ci/arm64`. A pull request from this repository runs again on each push; one from a fork runs when labelled, so remove and re-add the label to test new commits. Dependabot labels the updates that can break an arm64 guest (Hyperlight and the other crates of `hluk`, the guest-side wasmtime crates, the images' bases): see [`dependabot.yml`](../.github/dependabot.yml).

## Tier 3: nightly

`arm64.yml` again, at 03:17 UTC on `main`, on all three arm64 hosts. It is the only scheduled run on the self-hosted Linux arm64 host (tier 1's hosted arm64 runners boot no guest), a small VM under nested virtualization, where a guest's first touch of each page is slow. There the tests run with small guests, and Sunday's run adds the slow suites (agent, Java, node, .NET JIT, PowerShell) and the agent benchmarks, which take hours. The benchmarks of each nightly run are charted on the [benchmark pages](https://hyperlight-dev.github.io/hyperlight-unikraft).

A push to `main` starts no arm64 run: the next nightly tests it. To run it on a branch, start `arm64` by hand (Actions, Run workflow); `weekly` adds Sunday's suites.

[`arm64-report.yml`](../.github/workflows/arm64-report.yml) handles a failed nightly run:

1. It re-runs the failed jobs once.
2. Each job that fails again gets an issue labelled `ci/nightly`, titled `arm64 nightly: <job> fails`, with the job's link, the commit and the end of its log. Each later nightly run it fails in comments on that issue while the issue is open.

A job that reaches its timeout counts as failed; a run cancelled by hand does not. A run that does not start, because GitHub refused `arm64.yml` or an action it uses, gets an issue at once. The reporter runs when an arm64 run ends and every two hours, and reports each failed job once per attempt of a run.
