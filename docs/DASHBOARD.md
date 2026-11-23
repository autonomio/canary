# Research dashboard

Canary opens directly to a question or the last question you read in this tab.
The toolbar opens the **Questions** panel, where you can search and filter past
runs without leaving the current answer. **New question** returns to the inline
draft. **Research settings** opens the Sources and Scope steps. Choose OpenAlex
or an allowed local text library, then set the paper target, independent audit
correction limit, and any study brief or prior research. For public literature,
you can enable citation-guided selection and additional record sources.

**Review launch** asks the service to validate the declaration before you start.
It shows the effective models, sources, limits, and what each step does. Load a
Canary YAML declaration under Scope or paste or load research state JSON. Choose
whether to reassess the question with that context or build on earlier research.

Use **Search literature** in the toolbar to browse one page of OpenAlex
candidates without starting a research run. This uses the same recorded search
allowance. The results show publisher metadata and indexed abstracts where available.
The dashboard, CLI, and MCP clients use the same HTTP service.

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

With a browser, open the printed URL and wait for **Workspace connected** before
opening the **Questions** panel. Open **Menu → Service** to see the storage
directory, PDF extraction engine, Codex status, and API contract link.

In another shell at the checkout root, set `canary_preview_url` to the printed
address without its trailing slash. Use this browser-free smoke check to
fetch the dashboard and stylesheet, compile the example, and read the run list.

```sh
(
  set -eu
  curl -fsS -o /dev/null -w 'Dashboard HTTP %{http_code}\n' "$canary_preview_url/"
  curl -fsS -o /dev/null -w 'Stylesheet HTTP %{http_code}\n' "$canary_preview_url/assets/research.css"
  curl -fsS -w '\n' "$canary_preview_url/api/status"
  curl -fsS -w '\n' "$canary_preview_url/api/runs"
  CANARY_API_URL="$canary_preview_url" uv run --no-sync canary run docs/examples/research.yaml --dry-run
  curl -fsS -w '\n' "$canary_preview_url/api/runs"
)
```

A passing preview gives these results.

| Check | Expected result |
| --- | --- |
| Browser | **Workspace connected** and **No questions yet** in the Questions panel. **Service** shows temporary storage and either **Codex signed out** or **Codex unavailable**. |
| HTTP | Dashboard and stylesheet return `200`. Status reports `"service": "available"` and `"dashboard_enabled": true`. |
| CLI | The example resolves an audit with `gpt-6.1-sol` at `xhigh`, `budget_seconds: null` for an unlimited audit, and `max_correction_rounds: 3`. |
| Run list | `{"runs":[]}` before and after the dry run, confirming it created no run. |

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
