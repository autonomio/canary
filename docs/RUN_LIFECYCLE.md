# Research commands and lifecycle

The HTTP service owns application actions. `canary ask`, `run`, `bundle`, `auth`,
registry/log/control commands and retained compatibility commands are clients.
`canary runs serve` is service bootstrap. Clients never launch a research worker,
query a provider or read a local run registry to satisfy an application action.

```sh
canary ask "How do capsid amino acid changes affect folding and yield?"
canary runs show RUN_KEY
canary runs log RUN_KEY --tail 30
canary runs cancel RUN_KEY
canary bundle RUN_KEY
```

Submission is asynchronous and returns a run key. All commands accept `--port`;
`CANARY_RUNS_PORT` supplies the default. `ask --out` is replaced by service-owned
storage; retrieve the bundle through the API. YAML submissions/repetitions retain
the compiled models, reasoning effort, paper target and audit budget. Neither the
client nor cancellation changes the scientific reasoning settings or admits findings.

## Native run control

`POST /api/runs/{key}/cancel` requests cancellation and returns HTTP 202. The service
records the request, stops active deterministic tool workers, then the native worker
and its descendants (including separate process sessions). PID birth identities
prevent a stale registry row from targeting a reused PID. The native worker preserves
committed scientific files and available usage receipts, exports the partial view,
and seals a bundle when cleanup completes. Forced termination can leave an unsealed
partial bundle; it is never represented as scientific acceptance.

`cancelling` means work is stopping; `cancelled` is terminal. The dashboard offers
Cancel for live service-owned native workers. Container pause/resume remains the
retained container action and is not shown for native workers. Repeated reads do not
turn missing usage into zero or make a partial audit complete.

Native process ownership is recorded before the API returns the run key. On restart,
the service checks the recorded process identity instead of inferring liveness from
an old `researching` file. Dead workers with unfinished manifests become interrupted;
live workers remain observable and cancellable without their original in-memory
Popen handle. Detached workers survive a service restart; API tools need the service
available and expose a failed request if it is unavailable during that interval.

## Scientific tools

The native scientist/auditor receives a run-scoped HTTP tool command:

```sh
python -m canary.tools_cli --run RUN_KEY discover --plan question-plan-draft.json
python -m canary.tools_cli --run RUN_KEY acquire --ids W123 W456
python -m canary.tools_cli --run RUN_KEY catalog
python -m canary.tools_cli --run RUN_KEY source W123 --match "yield" --context 2
```

Each calls `POST /api/runs/{key}/tools/{action}`. The service resolves the workspace;
the caller supplies a question plan or selected IDs, never a source-body location or
provider credential. Discover/acquire/finalize and active source-read receipts execute
in tracked API-owned tool processes. Acquisition retains concurrent per-paper workers,
cursor history and individual failures. Research waits for its already-running source
tools before binding findings. Cancellation stops those processes before snapshotting.
Finished sources remain readable without appending to sealed scientific artifacts.

Network access is enabled for the native workspace sandbox so these local HTTP calls
can reach the service. This follows the [Codex network configuration](https://learn.chatgpt.com/docs/agent-approvals-security).
The fixed Sol/Astra models, reasoning effort, disabled delegation, source rules and
independent audit remain unchanged. The legacy `coverage_runtime` command also relays
through the API; its `--workspace` is an explicit service-host directory reference.
Prefer run IDs. Thread-usage inspection calls `/api/usage/threads/{id}`; the service
reads its shared credential/session context. `/api/log` and run-scoped receipts
retain their existing accounting views.

## Retained compatibility workflows and inputs

`POST /api/actions` accepts `{command, options}` for review/analyze/cycle/inspect/
resume/assess/revise/publish. The existing parser validates command fields and types;
the API launches its existing engine adapter asynchronously. Inspect returns a record
without launching science. These are retained workflows, not alternative model profiles
for the current compiled research path. Existing revision/promotion refusals remain.

`POST /api/inputs` accepts an octet-stream with Content-Length and returns an input ID.
The analysis CLI uploads its CSV, then references `input:<id>`; it does not assume the
client file exists on the service host. Engine outputs always use a new service-owned
workspace. Compatibility workflows that modify a historical bundle copy it first;
the original scientific files remain unchanged. Their accepted input/action packet is
retained so the API can repeat the action.

`POST /api/runs/import` accepts an explicit `server_path` plus optional name/brief.
`canary runs adopt --bundle SERVER_PATH` uses it. Existing registrations are reused
without erasing live ownership. Inspect/resume/assess may reference a registered run
key or an explicit service-host bundle directory. General browser uploads of whole
bundles are not implied by a client-side path. Local-footprint discovery follows in #125.

Engine unit tests call the internal engine adapter explicitly. Public command tests
exercise real HTTP requests, input uploads and source retrieval separately. Process
tests use synthetic workers/descendants and never constitute live scientific evidence.
