# Shared native execution

The API owns one long-lived Codex app-server over stdio, shared with its native
authentication connection. Each research and audit gets a separate fresh thread,
working directory, model, reasoning effort, tool environment and output schema.
Both scientific roles use Sol 6.1 xhigh without a duration limit. Disabled delegation, full-paper rules and
independent audit prompts remain unchanged.

Detached orchestration workers call `POST /api/runs/{key}/execution` with
`{"role":"research"}` or `{"role":"audit"}`. The service derives the prompt,
schema, model profiles and source-tool command from the active run. Neither
scientific stage has a duration limit. Clients cannot
supply native commands, credentials, model substitutions or workspace paths.
Each role is claimed atomically in the persistent run registry before native
initialization. Delayed or restarted duplicate requests cannot execute that role
again, including after working receipts have been released.
The response is the typed execution receipt; findings remain available through
the existing research and bundle endpoints. The operation is for orchestration;
CLI and MCP users continue to submit questions through the normal run API.

## Configuration compatibility

Codex 0.162.0 app-server has no equivalent of exec's `--ignore-user-config`.
Overriding a handful of settings does not remove every personal instruction,
MCP server or provider override. Canary therefore uses shared execution when the
native user configuration is empty or contains only credential-store selection.
A disabled configuration layer does not affect that choice. Otherwise it retains
the existing API-owned `codex exec --ignore-user-config` path. Missing configuration
layer evidence also retains that path. This is an explicit compatibility fallback;
research continues with its existing scientific configuration.

No configuration file is edited and no credentials are copied. Native Codex keeps
responsibility for file/keyring/auto credentials, refresh, browser/device login and
logout. Each role's invocation and receipt record `backend`, `fallback_reason`,
`configuration_policy`, version, native thread, exact native usage and cleanup.
The dashboard shows this information within the existing token/timing details.
`GET /api/status`, `canary status`, and MCP's `canary_status` expose the shared
process state and active shared/exec session counts. Availability is not a
certificate of model access or scientific validity.

## Cancellation and failure

Only pipe writes and request-ID allocation serialize. Responses route by request
ID; completed scientific items and turns route by thread ID. High-frequency token
and reasoning deltas are not copied into operational queues. Research tools still
use the existing run-scoped HTTP API; no source-acquisition scheduler is added.

Cancellation and audit timeout interrupt the selected native turn, never the
shared process. Existing tracked acquisition workers are stopped separately. Fallback process
identities are captured at launch, never recaptured when interrupting a child.
Completed threads are archived after their final output is received; native
receipts are collected after archival flushes the session. Findings, usage and
cleanup outcomes are committed before releasing the API's execution ownership.
Archival failures remain explicit and do not erase findings.

Before sealing, workers call `POST /api/runs/{key}/execution/finish` with a
boolean `cancelled` value. The API closes admission, interrupts outstanding work
and waits for native/tool writers to stop. The returned cancellation state wins
over a normal partial-audit response. SIGTERM cleanup uses this same handshake;
if writer shutdown cannot be confirmed, working files remain unsealed.

The shared server is a common failure domain. Its death wakes affected sessions;
Canary preserves partial files/receipts and does not silently repeat scientific
work. A service shutdown interrupts shared sessions and allows a bounded grace
period for receipts. Detached orchestration-worker liveness still survives API
restarts, but a shared scientific turn cannot survive losing its owning service.
A later service starts a new backend for new work, not an automatic research retry.

A failed role's receipt records `native_error` from the native session's final
`task_complete` event; `codex exec --json` prints only its message. `code` is the
verbatim `codex_error_info` and `message` is bounded and redacted. `server_overloaded`
is `transient_provider_capacity`, not a scientific refusal or research error; other
codes stay `unclassified`. The run API reports the research role's error, and
`/research` keeps each role's under `profile.roles`.

`same_run_retryable` is true only for `server_overloaded`. When a turn fails with it,
the API keeps the role's session registered and pauses for 30 s, doubling each time,
at most 600 s in total per role. It then resumes the same native thread on the same
model, account and workspace. The resumed turn's only input is a fixed continuation
instruction (`CAPACITY_RESUME_PROMPT`), so committed files, thread history and usage
receipts carry over. While a role waits, `GET /api/runs/{key}` reports `capacity_pause`
and the dashboard shows "Waiting for model capacity". Cancel, finish and service
shutdown interrupt the pause. The role receipt keeps every pause in `capacity_pauses`,
and the run timeline records each pause and resumption. When the bound is exhausted, the
role fails with its last `native_error`. A lost connection or a stopped session never
resumes. Other codes keep `same_run_retryable` false; `rerun` starts a new run with the
saved settings. No other model or account and no API-key fallback is used.

## Verification

The [native API smoke receipt](../validation/shared-codex-live-20261028.json)
records four concurrent sessions (two Sol xhigh, two Astra ultra) through the new
production backend in one-vCPU Linux arm64 Docker. Synthetic fixture prompts and
schemas replaced research content, so this did not start a literature review.
All four outputs matched; native receipts verified models/efforts and no child
agents; tools reached the canonical API; all native threads were archived.
The batch, including the finalization handshake, took 38.424 seconds. The receipt hashes identify the source snapshot
used for that live check; later lifecycle fixes have deterministic regression tests.
This is operational evidence, not research-quality or whole-question throughput.

The preceding controlled two-replay experiment measured native proportional
memory at eight waiting threads as 341 MiB for separate execs versus 175 MiB for
one app-server. That excludes paper acquisition/extraction and does not establish
capacity for eight complete reviews. Details were saved locally in
`~/.local/share/canary/performance/20261028/shared-codex/`.

The [official native protocol](https://learn.chatgpt.com/docs/app-server) documents
thread/turn controls and stdio. Codex remains pinned to 0.162.0 in the service image.
