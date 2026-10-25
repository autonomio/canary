# Compiled research and research bundles

Canary produces a research bundle. A consumer can inspect it, render it, or admit its contents into a world model. World-model identity resolution, promotion, maintenance and retirement belong to the consumer.

## Declare, compile, run

The CLI reads a user declaration and calls the local API. The API owns the single compiler and the existing scientific workers. JSON callers use the same compiler as YAML callers; no client-side approval flag bypasses it.

```sh
canary runs serve
canary run docs/examples/research.yaml --dry-run
canary run docs/examples/research.yaml
canary bundle RUN_KEY
canary runs rerun RUN_KEY
canary bundle RUN_KEY --connector resolvent
```

The [example declaration](examples/research.yaml) records question, exact research/audit model and effort, audit budget, coverage selection, paper target, fresh retrieval and accessible full-source policy. Omitted supported fields resolve to those same defaults. Positive targets/budgets are required; scarcity remains a recorded research outcome. Corpus reuse and other model profiles are not implemented, and are rejected explicitly.

Compilation rejects duplicate/unknown keys, executable YAML, invalid types, incompatible settings, unsupported versions/models/effort and subagents. It performs no search, extraction or model call. Credentials stay in service configuration/Codex; the declaration has no credential or executable-hook fields. Submission allocates a fresh API-owned workspace. Auth and submission use the owner-operated local-origin boundary; there is no hosted multi-user identity system.

A normal submission compiles once, stages the accepted input, and launches the native worker. Before its scientific session starts, that worker stores the original YAML, resolved settings and compiler report in the reserved bundle. Startup failures before this point retain the staged input and service launcher log; the API labels the input staged rather than inventing a completed manifest.

Repetition uses the saved declaration/resolved settings and verifies their digest, then creates a new run ID and fresh corpus. Unsupported old settings fail explicitly. This repeats configuration, not model output, search results or a previous software installation. Actual prompts, output schemas, CLI versions/invocations, Python, engine source fingerprints and dependency-lock digest remain inspectable. No model or effort fallback is applied.

## Storage contract, version 1

One directory is one run. Existing engine paths remain in place:

```text
bundle.json                  Version, persistent bundle ID, snapshot state, settings, artifact inventory
research.yaml                Exact submitted YAML (generated YAML for JSON/question callers)
run-spec.json                Fully resolved settings; stable SHA-256 identity
compile-report.json          Compiler version/checks, bundle identity/time and engine fingerprints
run.json                     Execution outcome; answer availability and audit scope remain separate
question-plan.json           Question concepts, search branches and inclusion criteria
selection.json               Candidate dispositions and selection rationale
coverage-map.json            Coverage and remaining needs
reviewed-papers.json          Actual source-reading record
papers/                      Saved original materials
texts/                       Canonical extracted text
passages/                    IDs, source IDs, text offsets, page markers and exact verbatim
payload.json                 Raw bound research graph, references and independent audit
investigation.json           Original scientific model output
answer.json, answer.md       Researcher-facing projection/report
research-*, audit-*           Prompts, schemas, events, invocations and usage receipts
profile.json                 Recorded scientific timing/usage
```

`bundle.json` uses `format: canary.research-bundle`, `version: 1`, a persistent `RB-<uuid>` ID, `created_at`, `state`, `execution`, `specification`, `artifacts` and `limitations`. An artifact has a relative path, byte count and SHA-256. Compilation inputs are inventoried at initialization; the complete inventory is sealed once the worker finishes, including failed runs. `finished` describes a terminal snapshot, not scientific acceptance. Execution status, unanswered scope and incomplete audits remain explicit.

The artifact inventory retains research files recursively, including CSV tables, figures, helper scripts and source/receipt directories. It excludes the manifest itself, hidden files/directories, credential filenames, temporary files, bytecode/dependency directories and symlinks. Service registry/staging/launcher logs and native Codex's own account/session store remain outside this directory; per-run scientific events and usage receipts are inside. There is no automatic machine-wide index: the current registry and the deferred local-footprint work are distinct (see #125).

The writer reserves new workspaces and never replaces prior source bodies. Input hashes are checked on bundle reads; artifacts are hash-checked before download/export. Downloading the ZIP streams its file contents instead of loading all papers into memory. These are inspectable filesystem snapshots, not protection against an operator rewriting both files and their checksums. Original and extracted text hashes differ by design. Exact passage reconstruction is `canonical_text[start:end]`; visual tables/figures and scientific interpretation still require original-source inspection and audit.

Old runs are `legacy_unversioned` with no fabricated YAML/specification. Their existing findings remain readable. Damaged/unsupported manifests are labeled invalid while the existing research projection remains available. No historical scientific files are rewritten to add this format.

## Findings

The shared researcher-facing heading is **Findings**. Current payloads retain their separate typed arrays:

| Type | Meaning | Connections and required context |
| --- | --- | --- |
| Claim | Attributed statement from the reviewed material | Uncertainty, freshness, evidence IDs and study-result IDs |
| Gap | What the scoped investigation cannot answer | Basis, claim IDs, evidence IDs, and `needed_to_resolve` |
| Residue | Relevant material that is neither a claim nor a gap, retained so it is not lost | Basis, claim IDs, evidence IDs, why it matters and scope; no required resolution or follow-up test |

Evidence records carry interpretation and a supports/qualifies/contradicts relation, separately from their verbatim. The chain is claim → evidence → canonical passage → reference → saved original. Gap/residue links use the same IDs; links can be empty where their basis is a scoped limitation rather than a quoted assertion. An absence statement applies to the inspected scope, not all literature. Contradictions retain linked claims/evidence; next questions retain gap/residue IDs. Older residue follow-up fields remain visible as legacy metadata.

Every newly generated claim requires ordinal uncertainty (`low=1`, `moderate=2`, `high=3`, rationale) and `freshness`:

```json
{"policy":"unknown","as_of":null,"review_after":null,"triggers":[],"rationale":"No defensible review horizon is available."}
```

Freshness policies are `scheduled`, `event_driven`, `historical`, `unknown`. `as_of` is the source claim's date scope (ISO date or null), not today's extraction date. Scheduled review requires an ISO `review_after`; event-driven review requires triggers. Historical/unknown/event-driven records have no scheduled date. Passing a horizon means reassessment is due, not that the claim becomes false. Do not invent dates or TTLs. Legacy claims display unknown freshness. The frontend separates uncertainty from review horizons; it does not lower confidence, hide claims, start a review or rewrite a bundle when a date passes.

## API/client contract

| Action | Request | Response |
| --- | --- | --- |
| Compile only | `POST /api/runs/compile` with `{"yaml":"..."}` or `{"spec":{...}}` | Exact source YAML, resolved spec, digest, compiler version/checks; no worker |
| Submit | `POST /api/runs` with the same declaration plus optional name/brief | New key, spec digest, bundle URL; asynchronous worker |
| Read bundle | `GET /api/runs/{key}/bundle` | Versioned bundle record and the existing research projection |
| Read artifact | `GET /api/runs/{key}/bundle/artifacts/{relative_path}` | Hash-checked bytes; paths outside the recorded bundle are refused |
| Download archive | `GET /api/runs/{key}/bundle/archive` | Finished snapshot ZIP with manifest and inventoried artifacts |
| Repeat settings | `POST /api/runs/{key}/rerun` with `{}` | A distinct run using the saved compiled settings |
| Consumer handoff | `GET /api/runs/{key}/exports/resolvent` | Resolvent source-material submission |

Invalid declarations return HTTP 400 with `code: invalid_research_spec` and located field issues. Compile/submit declarations require JSON; foreign browser origins/hosts are refused. Existing research/source endpoints remain usable. The frontend now loads the bundle endpoint and renders the API's projection, original/resolved settings, repeat action, artifact links and export actions. It never reads local run files directly.

Packaging and [run lifecycle](RUN_LIFECYCLE.md) now cover installation, native cancellation
and public CLI migration. OpenAPI/MCP and the login-free local-footprint dashboard
remain subsequent slices. The primary README remains the final delivery step in
[the API-first map](API_FIRST.md).

## Resolvent pilot and further consumers

The selected pilot is [autonomio/resolvent](https://github.com/autonomio/resolvent), contract revision `4b5a056b7686bcf1031fa48a3bde14dd58873834`. The old `Vaquum/Resolvent` URL redirects to the predecessor world model. The adapter normalizes a finished bundle into the current [source-material submission](https://github.com/autonomio/resolvent/blob/4b5a056b7686bcf1031fa48a3bde14dd58873834/docs/contracts/activity/model_input_source.json), validated in tests against the pinned destination schema. No new topic research or canonical world-model write occurs during export.

The handoff uses a stable `RUN-<bundle uuid>` ID, deterministic local ID mappings and exact source excerpts/offsets/hashes. Candidate claims retain their statements and uncertainty; `moderate` maps to Resolvent's `medium`. Candidate classes start as `derived_inference`; destination admission owns final classification. Evidence independence is explicitly unestablished. Relation kinds, original claim kinds, complete freshness records and independent audit scope remain in metadata and the original bound payload.

Resolvent currently has no native residue or claim-freshness field. Those records are preserved losslessly in the research artifact/metadata rather than converted into claims or resolution tasks. Its receiving admission process must account for them. Gaps likewise remain typed in the retained artifact. No acceptance criteria, pre-research brief or world-model relationships are invented to fill fields.

Pass the resulting JSON as `input-artefacts/RUN-<uuid>.json` to a chosen compatible world-model repository. Resolvent's PR-side admission supplies dispositions, runs `scripts/world_control.py` and its compiler, and owns independent review and publication. Its generic template is not a destination for research data. Retain the accompanying neutral bundle ZIP in the destination's artifact storage so source originals remain recoverable; the JSON handoff and public URLs alone do not transport all original files. This delivery supplies normalization/export, not an unrequested publication to a destination repository.

Further adapters can consume this same bundle version using small mapping functions; there is no plugin framework or destination-specific field in the producer format. Representative adjacent systems are [Graphiti](https://github.com/getzep/graphiti) (temporal knowledge-graph episodes, including structured JSON), [Cognee](https://docs.cognee.ai/api-reference/introduction) (document ingestion followed by graph construction), and [Mem0](https://docs.mem0.ai/api-reference/memory/add-memories) (agent memory with optional inference). These are integration comparisons, not a measured adoption ranking. A connector must preserve source material, uncertainty, freshness, scoped gaps and residue even when the destination's default ingestion would extract or rewrite facts.

Vectorization/embeddings of bundle artifacts are explicitly deferred in #126. A vector index is a derived retrieval view and must never replace the exact research bundle or source chain.
