# Canary operator runbook

Runbook for the retained contained-worker and Muse broker workflows.
Commands run from the checkout root unless stated otherwise.
For the current native research service, see the installation guide.
[Native installation](INSTALL.md)

## 1. Supported environments

| Environment | Offline suite | Acceptance | Containers | Live broker runs |
|---|---|---|---|---|
| macOS, Python 3.10–3.13, uv | yes | yes (containment skips without Docker) | only with Docker Desktop | no |
| Linux, Python 3.10–3.13, uv, Docker 28+ | yes | yes | yes | broker only |

`canary` is a console script (`canary.cli:main`). Workers never hold provider
or GitHub credentials on any platform.

## 2. Setup from a clean clone

Set up the locked checkout-local environment and run the portable suite
from the contributor guide. On Linux, `./scripts/container_verify.sh`
exercises the broker and guest boundary with fixture credentials.
[Checkout setup](../GUIDANCE.md#set-up-the-checkout)

## 3. Offline acceptance

```bash
canary_acceptance_dir=$(mktemp -d)
uv run --no-sync python scripts/run_acceptance.py --out "$canary_acceptance_dir"
```

The fixture scenarios cover verified bundles, cited reviews, CSV analysis,
cycles and crash/resume, revision refusal and hermetic revision logic,
budget and provider restrictions, containment, and export scanning.
Exit 0 means there were no failures. A scenario can skip when its host
requirements are unavailable. Read `verdicts.jsonl`, `manifest.json`, and
`ACCEPTANCE.md` in the chosen directory for the result of each scenario.

## 4. Trusted launch (Linux + Docker)

Deploy the broker once per host (sole holder of `MUSE_API_KEY`):

```bash
./scripts/gate_service.sh init
MUSE_API_KEY=<fresh-authorized-key> ./scripts/gate_service.sh start
```

Run workers only through the launcher (disposable container, no direct
egress, externally enforced limits):

```bash
./scripts/container_run.sh review "your question" --max-papers 10 --out /work/out
./scripts/container_run.sh cycle "your question" --max-iterations 3 --out /work/out
./scripts/container_run.sh inspect /work/out
```

Launcher knobs (host-side): `IMG`, `OUT`, `CANARY_NETWORK` (must stay
internal-only), `CANARY_GATE_NAME`, `CANARY_TIMEOUT_S` (default 1800),
`CANARY_WHEELS` (read-only `/wheels`), `CANARY_STAGE` (read-only `/inputs`),
`ALLOW_DIRTY=1` (logged override only). Each run writes `<out>.receipt.json`
with image digest, limits, and outcome. Guest shell is available only via
the explicit `exec` fixture hook, still contained.

## 5. Model preflight

Before any live run, confirm the broker (never the worker) is ready:

```bash
docker exec canary-gate python -m boundary.server preflight  # ledger + key usable
curl -H "Authorization: Bearer $CANARY_GATE_TOKEN" $CANARY_GATE_URL/v1/status
```

Workers need exactly two variables: `CANARY_GATE_URL` and
`CANARY_GATE_TOKEN`. A worker without a token refuses before any call; a
worker given provider keys directly is a misconfiguration — keys belong
only in the broker environment. Only `muse-spark-1.3-contributor` is
permitted; anything else is refused at the boundary.

## 6. Finite run specification

Every run validates a versioned spec before any external call. Budgets are
hard and shared across nested work, retries, and restarts:

| Field | Flag | Default | Meaning |
|---|---|---|---|
| `max_iterations` | `--max-iterations` | 3 (max 5) | research steps |
| `max_model_calls` | `--max-calls` | 25 | model dispatches |
| `max_tokens` | `--max-tokens` | 20,000,000 | rolling-window tokens |
| `wall_time_s` | `--wall-time-s` | 1800 | wall clock |
| `revise_rounds` | `--revise-rounds` | 1 (max 3) | revision rounds when enabled |

Terminal reasons are distinct and truthful: `converged`,
`budget_exhausted`, `insufficient_evidence`, `no_progress`,
`repeated_question`, `cancelled`, `provider_blocked`, `invalid_input`,
`failed`, `restart_required` (halt for a fresh worker after an accepted
revision; continue with `resume --expect-revision`).

## 7. Status

```bash
canary inspect <bundle>   # completed | <stopped reason> | interrupted | legacy | corrupt
```

Inspection never executes bundle content.

## 8. Stop and resume

- Stop: interrupt the run (Ctrl-C). In-flight work finishes; the bundle keeps
  a `cancelled` stop with completed iterations, checkpoints, and exact usage.
- `canary resume <bundle>` continues in place when worker code and inputs
  still hash-match the checkpoint, else refuses with the reason named.
- `canary resume <bundle> --fork <new-dir>` continues as an explicit fork
  (history copied, budgets restarted, parent recorded). Finished runs need
  `--fork`; legacy and non-cycle bundles cannot resume.
- `canary resume <bundle> --expect-revision <rev>` continues a
  `restart_required` leg research-only on a verified accepted revision.
  `--fork` and `--expect-revision` are exclusive.

## 9. Controlled export

Bundles leave containment only through the host-side exporter:

```bash
tar -cf - -C <bundle> . | python scripts/export_bundle.py <new-dir>
python scripts/scan_export.py <new-dir>   # exit 0: manifest valid, scan clean
```

The exporter copies bounded regular files into a NEW directory, rejects
traversal/links/devices/oversized payloads/collisions, and writes
`manifest.canary.json` with per-file sha256. The scanner re-verifies every
checksum and trips on credential-shaped text (best-effort, known prefixes).
Never import an export that fails either step.

## 10. Credentials: setup and rotation

- Broker: `MUSE_API_KEY` via environment at `gate_service.sh start` time
  (or a gitignored repo-root `.env`, auto-loaded only when the variable is
  unset; explicit environment wins); optional `OPENALEX_API_KEY`.
  Never commit keys; `.env` is gitignored.
- Worker tokens: minted per run from the broker (`mint-token`); revoke with
  `revoke-token --id <id>`.
- Rotation: mint a new provider key, restart the broker with it, revoke old
  worker tokens, confirm `/v1/status` on the new key. Never reset the token
  ledger volume to regain allowance (`canary-token-ledger-v1`).
- GitHub publishing uses a separately authorized maintainer token with
  `canary publish`; workers refuse to hold it (`verify_token_scope.sh`
  proves repo-only scope).

### Maintenance PR review

The maintainer publisher opens an `auto/revise-*` PR against `main` and waits
for completed checks and a current-commit approval from a different GitHub
account with repository write access. It submits the reviewed commit SHA with
the squash request. If review is missing, the issue and journal record the
reason and the PR stays open. On `autonomio/canary`, the publisher requires the
approval from the confirmed independent reviewer `bit-mis` (GitHub account ID
`265471408`). Other repositories continue to use their existing write-access
reviewers and publisher token.

On `autonomio/canary`, classic branch protection on `main` also requires one
approval, dismisses stale approvals, and requires up-to-date checks, including
for admins. The separate PR-only ruleset applies to the target branch.
Verify both controls with read-only requests:

```sh
gh api repos/autonomio/canary/branches/main/protection
gh api repos/autonomio/canary/rules/branches/main
```

The named reviewer is enforced by the publisher. GitHub rules target the
destination branch, and GitHub's required-reviewer rules select teams by file
pattern. Merges initiated separately in GitHub follow the destination
repository's protection settings.

## 11. Data-exposure policy

- Initial checks use only public or synthetic data.
- Bundles contain prompts, retrieved text, and model output: treat them as
  sensitive, redact before sharing, and never commit private data.
- Credential-shaped text is redacted at every persistence boundary, but
  redaction is best-effort pattern matching, not a guarantee.
- The acceptance bundle committed with a release is synthetic-only and
  secret-scanned; its scan log ships inside it.

## 12. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `refusing: revision has no trusted execution path` | `--maintenance`/`revise` outside tests | Expected: revision logic is exercised by the suite only; research runs are unaffected |
| `worker code changed since the checkpoint` | resume after code edit | Resume with `--fork`, or rerun from scratch |
| `journal has corrupt lines` | damaged journal | Inspect the bundle; start a new run (history is preserved) |
| `CANARY_GATE_TOKEN required` | worker without broker token | Deploy the broker (§4), preflight (§5) |
| `budget_exhausted` with work left | finite budgets working as designed | Raise the budget flags or resume the leftovers from `unanswered` |
| `no_progress` after two iterations | ungrounded output (citations without validated claims) | Check retrieval quality; grounded claims reset the counter |
| export scan findings | credential-shaped text in artefacts | Treat as a leak: rotate the credential, purge the export, find the source |
| `canary-*` containers left behind | interrupted launcher run | `docker ps -a`, remove only runs you own, then re-verify |

## 13. Known limitations

- Revision entry points refuse on every host; revision behavior is verified
  through the hermetic suite, not live promotion. No in-worker auto-merge.
- No live provider run has been made from this tree; latency, cost, and
  output quality claims are untested (release blocker, tracked separately).
- Guests have no direct egress: host, LAN, metadata, and public DNS are
  unreachable by design; literature arrives only via the broker.
- macOS runs the offline suite only: no containment, no revision.
- Green tests prove control flow, not sound inference, evaluator tamper
  resistance, or scientific truth.

## 14. Backlog

Open work lives in the issue tracker: guest HTTP/DNS posture (#4), offline
suite network guard (#30), and the live acceptance blocker filed with the
Build 17 release. The roadmap issue (#1) links the build sequence.
