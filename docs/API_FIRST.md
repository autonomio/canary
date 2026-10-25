# API-first delivery map

Canary is a research service. Its dashboard, CLI and agent tools use the same HTTP API and receive the same research records. This is the canonical map for the agreed migration; it is not a claim that the migration is already implemented.

## Boundary

```text
Dashboard / CLI / SDK / agent tools
                 |
          Canonical HTTP API
                 |
     Existing engine and owned workers
                 |
       Codex / OpenAlex / saved sources
```

Every application action enters through the API. Only API-owned engine code and workers may invoke vendors, launch scientific sessions, or manage run files. Internal engine functions can call each other directly; no internal loopback HTTP layer is required. Starting the service and installing its dependencies are bootstrap operations.

Authentication follows the same boundary: `canary auth login` calls the service, which owns the Codex login process and credential location. It must not sign in a separate client-side worker. Codex retains responsibility for its browser/device flow, credential storage and refresh. The service and its workers use the same persistent `CODEX_HOME`; credentials do not enter images, run artifacts or API responses. Login status establishes authentication, not access to every model or a successful research run.

Keep the tested stateful research/audit harness and the existing typed scientific payload. Do not substitute a single model API call, change scientific reasoning settings, add autonomy, or rewrite earlier research to implement this migration.

## Current baseline

Remote `main` at `3ac7e4f` has the coverage-guided Sol/Astra engine, deterministic acquisition, immutable source passages, typed findings and a working loopback HTTP dashboard. `POST /api/runs` already accepts a question; status, research, logs and source routes already exist. Preserve usable routes and data structures rather than renaming them for appearance.

Remaining bypasses include direct CLI research execution, direct scientific helper commands, local artifact reads by CLI commands and native Codex execution outside service ownership. The existing dashboard API has no published OpenAPI contract, native cancellation, or general cross-origin browser interface. Its current pause/unpause controls apply to containers. The Python wheel lacks the external launch/helper scripts and browser assets; the primary README, example environment and container recipe still describe the older workflow. See [the native runtime](COVERAGE_RUNTIME.md) and [current dashboard](DASHBOARD.md).

## Action inventory

The paths below reuse existing routes or name proposed additions. New paths remain proposals until their implementation lands with the contract.

| Action | Canonical API/client responsibility | Remaining work |
| --- | --- | --- |
| Start/inspect service | Local service bootstrap; expose service/runtime status | Supported installation, runtime discovery and startup command; distinguish service availability, login and run readiness |
| Authenticate | Proposed `/api/auth/*`; CLI and dashboard relay the same login session | API-owned Codex browser/device login, status and logout; persistent worker credentials; no custom OAuth implementation |
| Provide inputs/import runs | API-owned input staging and run adoption | Clients upload or reference inputs through the service and receive input/run IDs; retained analysis/adoption commands must not assume a client's local file exists on the server |
| Submit a question | Existing `POST /api/runs` with `kind: question` | Typed question/options request, run key response, explicit errors; no host paths needed by clients |
| Observe/control a run | Existing run/status/log routes; proposed native cancellation | Honest progress, elapsed time, failure/partial/audit states; cancel the actual owned process tree and preserve committed evidence |
| Search and obtain papers | Run-scoped API actions backed by existing discover/acquire/catalog functions | Scientific helper commands become API clients; retain concurrent acquisition, cursor history and per-paper failures |
| Read evidence and sources | Existing research and source context/file routes | Typed evidence-to-verbatim-to-reference navigation; preserve exact source slices, hashes and provenance diagnostics |
| Retrieve findings | Existing `GET /api/runs/{key}/research` | Publish the existing claims, uncertainty, study results, contradictions, gaps, residue, caveats and next-question schemas |
| Export results | API-owned export action/download | CLI/UI/agents request the same export; avoid client-side filesystem access or a separate report generator |
| Inspect usage/settings | Existing `/api/log` and source usage status, plus typed runtime settings | Consistent per-run tokens/durations/search counts; preserve unknown values and audit limits; keep provider secrets private |
| Use retained legacy commands | Same API boundary for retained review/analyze/cycle/inspect/resume/assess/revise/profile/publish/adopt actions | Inventory each command; route it through the API or expose its existing refusal/unsupported state explicitly; preserve existing behavior and do not revive refused code promotion |
| Use another frontend or agent | Same HTTP contract and result records | Backend-to-backend calls work first; add allowed-origin browser access only where needed; clients use the deployment's explicit access boundary |

## Ordered delivery

Complete each item’s API behavior, client integration and meaningful acceptance evidence together; do not defer the frontend or agent view to a later cleanup.

1. [x] **Service and authentication.** API-owned native Codex browser/device login, status, progress, cancellation and logout; HTTP-only CLI and dashboard clients; one persistent credential context shared with native workers. The current access boundary is the owner-operated loopback service with same-origin authentication controls. See [the auth contract and workflow](AUTH.md); provider login and access to Canary remain separate concerns.
2. [ ] **Installable execution.** Package required Python entry points/resources, the supported CLI runtime and extraction dependencies. Remove source-checkout path assumptions. Supply a container for the current pipeline with persistent auth and research storage. Keep the dashboard optional for headless use and include its assets when installed.
3. [ ] **Research and run lifecycle.** Make `canary ask` and retained application commands thin API clients. Route scientific acquisition/source/finalization tools through the service. Add native cancellation and any explicitly requested run limits without silently changing coverage or reasoning. Persist enough ownership/state for honest status after service restarts.
4. [ ] **Canonical contract.** Publish OpenAPI and runnable examples for submission, polling, cancellation, results, exports, usage and source/evidence access. Reuse the existing research schemas and diagnostics. Verify that another application can render the findings using only API responses.
5. [ ] **Agent tools.** Provide a small MCP adapter over the same API: authenticate/status, ask, inspect a run, cancel, retrieve findings and inspect evidence/source context. Keep server tools and schemas aligned with OpenAPI; no second orchestration implementation or direct artifact access.
6. [ ] **Fresh deployment proof.** From a clean install, start the service, authenticate, submit a fresh question, follow progress and retrieve its answer and exact cited source context. Exercise cancellation and restart behavior. Record actual timing and token usage; separate offline fixtures from live scientific evidence and leave scientific acceptance to the recorded audit scope.
7. [ ] **README last.** Follow [Vaquum/Limen's structure](https://github.com/Vaquum/Limen): purpose, choose your path, install, runnable examples and deeper reference links. Describe the now-working API, CLI, optional dashboard and agent paths; align `.env.example` and Docker instructions. Clearly label retained legacy behavior.

## Completion evidence

- A fresh installation can execute the documented workflow without unpublished source, a developer-specific path or manual patching.
- Dashboard, CLI and MCP callers exercise the same API actions and receive the same typed records. No retained application command silently bypasses the service.
- Another application can submit a question and navigate claims, evidence, exact verbatim and original references without reading Canary's filesystem or importing its internals.
- Failures, cancellation, unavailable sources, missing provenance, unknown usage and partial audits stay visible. Existing scientific artifacts remain unchanged.
- Required local checks and CI pass for the published changes; actual review findings are handled and reviewed PRs are merged. Update this map with implementation links and evidence as each item lands.
