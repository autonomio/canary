# Research dashboard

The dashboard lists questions and shows progress, findings, and source
evidence from the same HTTP service used by the CLI and MCP clients.

## Quickstart

For a credential-free preview, set up the checkout and run the service in
its own shell from the checkout root. The preview uses temporary research
and authentication directories and lets the operating system pick a port.
[Checkout setup](../GUIDANCE.md#set-up-the-checkout)

```sh
canary_preview_dir=$(mktemp -d)
CANARY_DATA_DIR="$canary_preview_dir/data" \
  CODEX_HOME="$canary_preview_dir/auth" \
  CANARY_RESEARCH_ROOTS= CANARY_RUNS_PORT=0 \
  uv run --no-sync canary runs serve
```

Open the printed URL. **Questions** should show zero runs. Open
**Menu → Service** to see the storage directory, PDF extraction engine,
connection status, and link to the API contract.

In another shell at the checkout root, set `canary_preview_url` to that
printed address without its trailing slash. Check the same service from the
CLI and HTTP.

```sh
curl -fsS "$canary_preview_url/api/status"
CANARY_API_URL="$canary_preview_url" uv run --no-sync canary run docs/examples/research.yaml --dry-run
curl -fsS "$canary_preview_url/api/runs"
```

The dry run prints the resolved settings and the run list stays empty.
Stop the preview with Ctrl-C in its service session. For a persistent service
and live questions, follow the installation and authentication guides.
[Installation](INSTALL.md) · [Authentication](AUTH.md)

## Read a research run

Open a native research question to see **Answer**, **Papers**, and
**Search net**. In **Answer**, follow a claim to its supporting evidence and
saved source context. Candidate findings can be provisional during research
or correction. Inspect audit results and **Run issue** when it appears for
withheld claims, incomplete checks, and limits on coverage.
[Findings and source reconstruction](API.md#findings-and-exact-source-reconstruction)

The workspace menu opens the operational log and local footprint. The
footprint describes the configured server-side directories and missing
bundles. [Local footprint](LOCAL_FOOTPRINT.md)

## CLI mirror

With a running service, `canary runs list` lists questions,
`canary runs show RUN_KEY` reads the status, and
`canary runs log RUN_KEY` reads the recorded execution log.
`canary bundle RUN_KEY` reads the bundle. Run controls follow the recorded
lifecycle and capabilities of each run. [Run lifecycle](RUN_LIFECYCLE.md)

## Retained container controls

Container runs can expose pause and resume in the dashboard. Their legacy
launcher settings, receipts, and limits are covered by the operator runbook.
On macOS, `bash scripts/dashboard_service.sh start` installs a persistent
LaunchAgent and requires Docker. Use `bash scripts/dashboard_service.sh stop`
to remove that agent. [Operator runbook](RUNBOOK.md)
