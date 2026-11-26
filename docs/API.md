# HTTP and agent interfaces

The live contract is `GET /openapi.json`, also printed by `canary api`. It describes
submission, compilation, polling, control, findings, exact source context, bundle
downloads/exports, authentication, inputs, retained actions and usage. The service
and every client use the same routes; no SDK or filesystem access is needed.

```sh
canary runs serve
canary connect
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
A `needs_clarification` run carries the researcher's `clarification` question instead
of findings; a `failed` run carries its recorded `failure` reason.
The bundle includes original/resolved settings and limitations. See
[run lifecycle](RUN_LIFECYCLE.md) and [bundle storage/ontology](RESEARCH_BUNDLE.md).

A question declaration can also supply `spec.study_brief`, a text field for the study's
design, comparators, outcomes and known references. Canary preserves it with the
original declaration and uses it as research context for the submitted question.
The dashboard, `canary run` and MCP submit the same field.

For public literature runs, use `canary ask "Your question" --citation-graph`
or set `retrieval.selection: citation_graph` in the declaration. The service
ranks up to three additional candidates from the run's OpenAlex reference links
and shared keyword searches. Acquisition requires a recorded decision for each
lead. The dashboard and `research.coverage.citation_leads` show the citation
paths and pending decisions. Supply `--study-brief` on `canary ask` to add context.

Use `POST /api/sources/openalex/search` with `{"query":"lake water quality","limit":15}`
for one page of candidate metadata without starting research. It honors the service's
recorded OpenAlex hold and returns the match count, upstream status and any retry
deadline. The toolbar, `canary sources search`, and MCP use the same route.

## Additional public sources

Set `retrieval.additional_sources` to any combination of `clinicaltrials`, `epo`
and `osf` in the existing `spec` declaration. Omission preserves OpenAlex-only
behavior. CLI `--add-source` flags and dashboard checkboxes submit this same field;
MCP receives it through the shared OpenAPI declaration. Source status and limitations
are exposed as `research.coverage.additional_sources` (`AdditionalSourceStatus`).
References retain provider-specific source kinds and metadata. These documents do
not count as full publications. See [scope, credentials and receiving semantics](OPTIONAL_SOURCES.md).

## Local text input

Set `retrieval.corpus: local_text` and `retrieval.local_text.directory` in the same
submission. Optional `retrieval.local_text.metadata` binds JSONL bibliographic
records. The service requires an explicit `CANARY_LOCAL_TEXT_ROOTS` allowlist.
See [local input, provenance and retrieval limits](LOCAL_TEXT.md). The live
OpenAPI declaration and MCP submission schema publish these fields.

## Review decisions

To see why a paper was excluded, open **Research bundle → Review decisions**.
Each entry gives the reason and cites the evidence. It shows who proposed,
made or checked the decision and whether a person or software did the work.
Reviewers supply their own names and roles.

Each new decision or correction creates an edition. Choose an earlier edition
to see what the record said at the time. You can download any edition as JSON.

| Action | Command |
| --- | --- |
| Read the latest edition | `canary decisions RUN_KEY` |
| Read an earlier edition | `canary decisions RUN_KEY --edition ENTRY_ID` |
| Submit a decision or correction | `canary decisions RUN_KEY --submit FILE` |

The dashboard accepts the same JSON file. In this example, a review requires
a measure of tensile strength. Report 17 measures stiffness alone, so the
reviewer excludes it.

```json
{
  "request_id": "screen-report-17",
  "expected_last_entry": "initial",
  "replaces": null,
  "decision": {
    "subject": "Report 17",
    "judgement": "exclude",
    "status": "accepted",
    "reason": "The review requires tensile strength. This paper reports stiffness alone.",
    "evidence": ["Report 17, Methods, paragraph 2"],
    "proposed_by": null,
    "decided_by": {"kind": "person", "name": "A. Lee", "role": "screener", "identity": "self_declared"},
    "checked_by": null
  }
}
```

Canary returns the new entry’s ID in `last_entry`. Pass that value to
`--edition` to reopen the edition. If you lose the response, send the same
request again to recover the ID.

For each new request, choose a new `request_id` and copy the latest edition’s
`last_entry` into `expected_last_entry`. To correct an earlier decision, put
that decision’s ID in `replaces`. The original stays on record. If Canary
returns HTTP 409, read the latest edition and check your request against it.

Use the same subject for a correction. To fix a spelling error or change a
display name, include `subject_id` inside `decision`. Copy the subject ID shown
in the dashboard. For older entries that omit this field, follow `replaces`
back to the first decision and use its entry ID. For a new decision, you can
use the permanent ID of the report or study.

A decision request can contain up to 65,536 bytes of JSON. A larger request
returns HTTP 413. Reading decisions from a bundle with no versioned manifest
returns HTTP 400. HTTP 404 means the bundle or edition could not be found.
HTTP 411 means the request needs a Content-Length header. HTTP 500 means
Canary could not read or save the review history.

When you review an imported bundle, Canary keeps your decisions in its own
data directory under the bundle’s permanent ID. If you move the original
and import it again, its decisions follow that ID. An imported correction
becomes part of the saved history if it continues the same sequence. If the
two copies disagree, the request returns HTTP 409 and both histories remain
intact. The download includes the saved decisions and the original research.

Save an edition’s last entry ID separately if you need to check that the
edition is still present after restoring a backup.

Both HTTP actions use `/api/runs/{key}/review-decisions`. GET reads the latest
edition, or an earlier one if you pass `last_entry`. POST accepts the JSON
shown above. The corresponding MCP tools are `canary_review_edition` and
`canary_record_review_decision`. For the original research and audit rounds,
use `GET /api/runs/{key}/revisions/{round}/{kind}`.

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

New completed requests retain the research bundle and release working papers and
full text. `bundle.retention.mode` is `bundle_only`; the downloadable artifact is
`resolvent.json`, directly accepted as a Resolvent contribution while
`bundle.contribution.within_limit` is true. Above Resolvent's 10 MiB file limit the
export is refused with HTTP 422 `contribution_too_large`. The same bundle
contains the dashboard projection and repeat settings. Source context then returns
`scope: excerpts`, `text_complete: false`, `offset_verified: false` and no local
file link. Cited verbatim, source URLs, hashes and recorded offsets remain; full
original bytes are not part of this retention contract. Active requests and
historical full-workspace bundles still expose saved full-source context.

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

`GET /api/connection` (MCP: `canary_connection`) reports the service-host Codex
version, authentication method and resolved credential store. Login challenges are
excluded. `canary connect` reuses an existing login or starts API-owned sign-in;
only a stopped local service is bootstrapped. See [connection and authentication](AUTH.md).

For a separate browser frontend, set `CANARY_ALLOWED_ORIGINS`, for example
`http://localhost:3000`. CORS is opt-in, with exact origins and JSON preflight support.
Native login/status/logout do not receive cross-origin browser access; use the
CLI/service authentication flow. Host and origin configuration belong to the service,
not a submitted research declaration. Keep the deployment's access boundary private.

`tests/test_api_contract.py` exercises the real HTTP service and stdio MCP client:
compile → submit → findings → claim/evidence/passage → broader context → original
material, without reading the research filesystem in the client. Its science is
explicitly synthetic; the subsequent deployment proof records live research separately.

Question submissions also accept optional `research_state` (a JSON object, at most 10 MiB) and `continue_research` (boolean, default false), under `spec` or alongside `kind: question`. A true continuation flag requires a nonempty state. The API preserves the exact starting state; the CLI YAML declaration and MCP contract expose the same fields. See [continuation inputs](RESEARCH_BUNDLE.md#continue-from-supplied-research-state) for paper-coverage and unchanged-workflow semantics.
