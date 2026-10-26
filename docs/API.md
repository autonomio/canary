# HTTP and agent interfaces

The live contract is `GET /openapi.json`, also printed by `canary api`. It describes
submission, compilation, polling, control, findings, exact source context, bundle
downloads/exports, authentication, inputs, retained actions and usage. The service
and every client use the same routes; no SDK or filesystem access is needed.

```sh
canary runs serve
canary auth login
canary api > canary-openapi.json
curl -s http://127.0.0.1:8789/api/runs \
  -H 'Content-Type: application/json' \
  -d '{"spec":{"question":"How do capsid amino acid changes affect folding and yield?"}}'
curl -s http://127.0.0.1:8789/api/runs/RUN_KEY
curl -s http://127.0.0.1:8789/api/runs/RUN_KEY/bundle
```

Submission returns HTTP 201 and a run key. Poll that key while it is active; inspect
its returned controls before cancellation. Terminal execution does not certify that
the question was answered, coverage was complete, or every claim passed its audit.
The bundle includes original/resolved settings and limitations. See
[run lifecycle](RUN_LIFECYCLE.md) and [bundle storage/ontology](RESEARCH_BUNDLE.md).

## Local text input

Set `retrieval.corpus: local_text` and `retrieval.local_text.directory` in the same
submission. Optional `retrieval.local_text.metadata` binds JSONL bibliographic
records. The service requires an explicit `CANARY_LOCAL_TEXT_ROOTS` allowlist.
See [local input, provenance and retrieval limits](LOCAL_TEXT.md). The live
OpenAPI declaration and MCP submission schema publish these fields.

## Findings and exact source reconstruction

`research.answer` is the researcher-facing graph. Follow these IDs:

```text
claim.evidence_ids → answer.evidence[].id
evidence.paragraph_id → answer.passages[].id
passage.source_id → answer.references[].id
```

Request `/api/runs/{key}/sources/{source_id}/context?paragraph_id=ID&scope=section`
to read the selected quotation inside its saved section/page. Use `scope=paper` for
the full saved readable paper. `offset_verified`, `text_complete`, `warning` and
binding issues stay visible. `local_file_url` retrieves the saved original; it never
substitutes an abstract when full text is missing. Resolve relative URLs against the
API origin, not the application's frontend origin. Source offsets address canonical
extracted Unicode text; PDF originals have their own content hash and page context.

OpenAPI publishes the existing `GeneratedInvestigation`, `GeneratedClaim`,
`GeneratedEvidence`, `Gap`, `Residue`, `StudyResult`, `Contradiction`, `NextQuestion`,
`Freshness` and audit schemas. Current generation requires claim uncertainty and
freshness. Recorded API views also preserve legacy/incomplete values and provenance
failures; their looser recorded-record schemas do not silently repair old research.
Residue retains relevant material without a resolution requirement.

The log is `GET /api/log`: start/end, duration, papers, answer availability and token
accounting. Unknown values remain null. Input includes cached input; cached tokens
are a subset, not an additional charge to add to total input. Native output includes
reasoning where reported. The original usage receipts/profile remain in the bundle.

## MCP

Start the HTTP service first. Configure any stdio MCP client with:

```json
{"mcpServers":{"canary":{"command":"canary","args":["mcp"]}}}
```

Or run `canary mcp --port 8789`. The relay fetches `/openapi.json` at initialization;
its tool names/input/output schemas come from that live contract. It only calls HTTP:
it has no scientific worker, provider or artifact-file implementation. Calls return
structured JSON and preserve HTTP errors as tool errors. It does not start the service
or authenticate on initialization. Authentication tools return the native challenge;
a human completes login. Cancellation, compilation, question submission, inspection,
findings, source context, bundles, exports and usage use the same service actions.

The [official Python SDK's supported 1.x line](https://github.com/modelcontextprotocol/python-sdk/tree/v1.x)
preserves Canary's Python 3.10 support; the dependency is bounded below 2 because
[SDK 2 requires Python 3.11](https://py.sdk.modelcontextprotocol.io/). No model
inference or separate orchestration is introduced by the adapter.

## Another application or host

`CANARY_API_URL` selects an explicit HTTP(S) origin for CLI/MCP clients, otherwise
they use loopback and `--port`/`CANARY_RUNS_PORT`. The full origin takes precedence
over the loopback port. URL credentials, query strings and path prefixes are not
supported. Native workers are explicitly pointed at their owning service.

The API remains an owner-operated private service; this adds no hosted multi-user
account system. For backend-to-backend traffic using another Host authority, set
`CANARY_ALLOWED_HOSTS` on the service to the exact authorities, such as
`canary:8789`. Defaults remain loopback. Compose passes this setting through.

For a separate browser frontend, set `CANARY_ALLOWED_ORIGINS`, for example
`http://localhost:3000`. CORS is opt-in, with exact origins and JSON preflight support.
Native login/status/logout do not receive cross-origin browser access; use the
CLI/service authentication flow. Host and origin configuration belong to the service,
not a submitted research declaration. Keep the deployment's access boundary private.

`tests/test_api_contract.py` exercises the real HTTP service and stdio MCP client:
compile → submit → findings → claim/evidence/passage → broader context → original
material, without reading the research filesystem in the client. Its science is
explicitly synthetic; the subsequent deployment proof records live research separately.
