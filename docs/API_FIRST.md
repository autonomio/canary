# API-first delivery map

Canary is a research service. Its dashboard, CLI and agent tools use the same HTTP API and receive the same research records. This is the canonical map and delivered contract. The checklist separates completed actions from deferred work.

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

## Initial baseline before delivery

Remote `main` at `3ac7e4f` has the coverage-guided Sol/Astra engine, deterministic acquisition, immutable source passages, typed findings and a working loopback HTTP dashboard. `POST /api/runs` already accepts a question; status, research, logs and source routes already exist. Preserve usable routes and data structures rather than renaming them for appearance.

The initial bypasses included direct CLI research execution, direct scientific helper commands, local artifact reads by CLI commands and native Codex execution outside service ownership. That initial dashboard API lacked a published OpenAPI contract, native cancellation and general cross-origin browser access; the initial wheel omitted launch/helper scripts and assets. The delivered capabilities are recorded below. The primary README and example environment still await their separately deferred rewrite. See [the native runtime](COVERAGE_RUNTIME.md) and [current dashboard](DASHBOARD.md).

## Action inventory

The paths below reuse existing routes or name proposed additions. New paths remain proposals until their implementation lands with the contract.

| Action | Canonical API/client responsibility | Remaining work |
| --- | --- | --- |
| Start/inspect service | Local service bootstrap; expose service/runtime status | Supported installation, runtime discovery and startup command; distinguish service availability, login and run readiness |
| Authenticate | Proposed `/api/auth/*`; CLI relays the service-owned login session | API-owned Codex browser/device login, status and logout; persistent worker credentials; no custom OAuth implementation |
| Provide inputs/import runs | API-owned input staging and run adoption | Clients upload or reference inputs through the service and receive input/run IDs; retained analysis/adoption commands must not assume a client's local file exists on the server |
| Submit a question | Existing `POST /api/runs` with `kind: question` | Typed question/options request, run key response, explicit errors; no host paths needed by clients |
| Observe/control a run | Existing run/status/log routes; proposed native cancellation | Honest progress, elapsed time, failure/partial/audit states; cancel the actual owned process tree and preserve committed evidence |
| Search and obtain papers | Run-scoped API actions backed by existing discover/acquire/catalog functions | Scientific helper commands become API clients; retain concurrent acquisition, cursor history and per-paper failures |
| Read evidence and sources | Existing research and source context/file routes | Typed evidence-to-verbatim-to-reference navigation; preserve exact source slices, hashes and provenance diagnostics |
| Retrieve findings | Existing `GET /api/runs/{key}/research` | Publish the existing claims, uncertainty, study results, contradictions, gaps, residue, caveats and next-question schemas |
| Export results | API-owned export action/download | CLI/UI/agents request the same export; avoid client-side filesystem access or a separate report generator |
| Index local research | `GET /api/footprint`, `POST /api/footprint/refresh`; CLI and MCP use the same actions | [Configured historical roots and local dashboard](LOCAL_FOOTPRINT.md); preserve ownership, unknown activity and missing folders |
| Inspect usage/settings | Existing `/api/log` and source usage status, plus typed runtime settings | Consistent per-run tokens/durations/search counts; preserve unknown values and audit limits; keep provider secrets private |
| Use retained legacy commands | Same API boundary for retained review/analyze/cycle/inspect/resume/assess/revise/profile/publish/adopt actions | Inventory each command; route it through the API or expose its existing refusal/unsupported state explicitly; preserve existing behavior and do not revive refused code promotion |
| Use another frontend or agent | Same HTTP contract and result records | Backend-to-backend calls work first; add allowed-origin browser access only where needed; clients use the deployment's explicit access boundary |

## Ordered delivery

The current owner-approved delivery uses one PR per step: (1) installable deployment,
(2) remaining API/lifecycle migration, (3) OpenAPI and MCP interfaces, (4) clean-install
live proof, (5) Resolvent receiving-side proof in a chosen instance, (6) login-free
local-footprint dashboard (#125). Bundle embeddings (#126) and the final README
rewrite follow later. The detailed capabilities below remain the acceptance map;
the two interface items are delivered together in this six-PR sequence.

Complete each item’s API behavior, client integration and meaningful acceptance evidence together; do not defer the frontend or agent view to a later cleanup.

1. [x] **Service and authentication.** API-owned native Codex browser/device login, status, progress, cancellation and logout; HTTP-only CLI authentication; the local dashboard has no provider login controls; one persistent credential context shared with native workers. The current access boundary is the owner-operated loopback service with same-origin authentication controls. See [the auth contract and workflow](AUTH.md); provider login and access to Canary remain separate concerns.
2. [x] **Installable execution.** The wheel packages the native launcher, dashboard and lock fingerprint; native scientific tools use installed Python modules. A configurable data root is separate from package resources. [Installation](INSTALL.md) covers Codex, Poppler, API-only startup and the current service container with persistent auth/research volumes. CI exercises a fresh wheel outside the checkout and container replacement. [Live deployment proof](DEPLOYMENT_PROOF.md) records the actual runtime, token use and partial scientific audit.
3. [x] **Research and run lifecycle.** The [compiled YAML and versioned bundle slice](RESEARCH_BUNDLE.md) provides compilation/submission/repetition, bundle ingestion and a Resolvent handoff. [Run lifecycle](RUN_LIFECYCLE.md) makes the remaining public commands API clients, routes deterministic scientific tools through the service, adds native process-tree cancellation and persisted ownership for restart reconciliation. Existing compatibility engines/refusals remain; input CSVs are uploaded and historical write workflows use copies. Scientific settings are unchanged. No new run limits were requested.
4. [x] **Canonical contract.** [HTTP and agent interfaces](API.md) publishes `/openapi.json`, `canary api`, typed scientific schemas, examples and explicit private-host/browser-origin configuration. Contract tests navigate findings to the exact saved source using only HTTP responses.
5. [x] **Agent tools.** `canary mcp` provides a stdio relay whose tools/schemas come from the live HTTP contract. It owns no engine, provider or artifact access. Real SDK client tests exercise compilation, submission, findings, source reconstruction and errors through the API.
6. [x] **Fresh deployment proof.** From a clean install, start the service, authenticate, submit a fresh question, follow progress and retrieve its answer and exact cited source context. Exercise cancellation and restart behavior. [Actual deployment evidence](DEPLOYMENT_PROOF.md) records timing/tokens, restart, cancellation and HTTP source reconstruction. The scientific audit remained partial at its time budget; fixtures are separate from live evidence.
7. [ ] **README last.** Follow [Vaquum/Limen's structure](https://github.com/Vaquum/Limen): purpose, choose your path, install, runnable examples and deeper reference links. Describe the now-working API, CLI, optional dashboard and agent paths; align `.env.example` and Docker instructions. Clearly label retained legacy behavior.

## Completion evidence

- A fresh installation can execute the documented workflow without unpublished source, a developer-specific path or manual patching.
- Dashboard, CLI and MCP callers exercise the same API actions and receive the same typed records. No retained application command silently bypasses the service.
- Another application can submit a question and navigate claims, evidence, exact verbatim and original references without reading Canary's filesystem or importing its internals.
- Failures, cancellation, unavailable sources, missing provenance, unknown usage and partial audits stay visible. Existing scientific artifacts remain unchanged.
- Required local checks and CI pass for the published changes; actual review findings are handled and reviewed PRs are merged. Update this map with implementation links and evidence as each item lands.
