# Coverage-guided question research

The native operator path uses one stateful **Sol 6.1 / xhigh** research session and one **Astra 6 / ultra** independent audit. Neither scientific session may delegate. Coverage-guided selection is the default: choose question-relevant source batches by the information they add across the typed plan's comparison, outcome, population and condition aspects, including adverse and null findings.

```sh
canary ask "Your question" --out out/questions/new-question
# Dashboard launches the fixed script with OUT supplied by its supervisor:
OUT=out/questions/new-question bash scripts/coverage_run.sh ask "Your question"
```

`canary ask` is the default question path and exports the source graph for the dashboard. Legacy review/cycle commands remain separate. When UI and engine use different checkouts, set `CANARY_QUESTION_ENGINE_ROOT` on the dashboard; clean checkouts use their own root.

The workspace must be new. The researcher writes a typed `question-plan.json`, runs full-text-inclusive OpenAlex discovery, screens titles/abstracts, chooses batches, and reads the acquired full sources in the same session. The plan uses **concepts**, not entities. Typed rich plans retain population, efficiency denominators, eligibility and coverage facets; no fields are discarded. OpenAlex `search=` includes indexed full text, titles and abstracts; it is not a body-only search and coverage depends on provider indexing.

Code owns network discovery, public-copy resolution, concurrent download, text extraction, immutable source paragraphs, reproducible quote placement, hash/graph checks, timestamps and usage reconciliation. It does not make semantic selection decisions or author scientific findings. Only selected batches are downloaded; sources become available as individual receipts commit, without waiting for a whole corpus.

Public-copy validation passes its extracted text, page count and blank-page list directly to acquisition; the winning copy is not converted again. Acquisition receipts record `validated_text_reused`. Original bytes, text normalization, page markers and passage offsets follow the same existing path. The extracted result exists only with that copy during acquisition, with no persistent body cache.

PDF parsing uses one shared conversion slot per service data directory, including the native `pdftotext` path, Python fallback and PDF identity checks. Downloads remain concurrent. The API passes its data directory to every tool worker; an OS file lock coordinates separate processes and threads. Native converters inherit the lock descriptor so a parent crash cannot release capacity while its converter still runs. Process exit releases ownership; no lock cleanup daemon is needed. The zero-byte `runtime/pdf-conversion.lock` file remains in place to preserve lock identity. `/api/status` publishes the extraction engine and capacity scope; the dashboard's service details show the shared slot. This bounds simultaneous PDF parsers, not total server CPU or complete research duration.

The minimum target is **50 substantively reviewed question-relevant publications**. Core comparisons and supporting context are separate; source versions count once and linked experiment reports retain study-family IDs. An inaccessible copy, elapsed time or an unexamined source does not establish scarcity. When genuine scarcity prevents the target, the researcher records assessed exhaustion, follow-up searches and access limits. The report exposes this statement rather than certifying it automatically or padding the corpus.

Standalone deterministic commands (no model call):

```sh
.venv/bin/python scripts/run_coverage_research.py discover --workspace PATH --plan PATH/question-plan.json --pages 5
.venv/bin/python scripts/run_coverage_research.py acquire --workspace PATH --ids W123 W456
.venv/bin/python scripts/run_coverage_research.py catalog --workspace PATH
.venv/bin/python scripts/run_coverage_research.py source --workspace PATH W123 --match "methods|results" --context 2 --limit 8
.venv/bin/python scripts/run_coverage_research.py finalize --workspace PATH
```

`discover` saves fresh responses and cursor progress. Its per-call page limit is explicit; truncated branches remain incomplete and the next call resumes them. Supplemental branches may be added under new query IDs; changing an existing query's text requires a new ID. The operator daily-search count includes successful cursor pages from this runtime and explicitly excludes the gateway and other clients. Selected public body acquisition makes no paid OpenAlex content request. Failed papers receive individual receipts; successful canonical bodies are not overwritten. `--retry-failed` records previous failure receipts when an operator requests another attempt in the same run.

`papers/` preserves originals; `texts/` contains native `pdftotext -layout` output where available, with the existing reader fallback and explicit PDF page markers. Scanned/unreadable sources fail extraction; unknown glyphs remain visible. Tables retain text layout, but images, figures, mathematical notation and ambiguous tables still require original-source inspection. A retrieved source's status says `requires_source_verification` or `preview_suspected`, never automatic full-paper scientific certification.

The payload stores the full question plan, selection decisions, coverage map, actual reading notes and the chain **prose → claims → evidence → canonical verbatim → references**. Claims carry ordinal uncertainty (low=1, moderate=2, high=3), not calibrated probabilities. Study-result fields cite their supporting spans or remain null. Contradictions, gaps, residue, caveats and justified next questions remain typed.

Raw `investigation.json` remains unchanged. The independent audit is separate; checked and unchecked claims and its limits are exposed. The default audit budget is 600 seconds, with actual start/end and termination recorded. A completed research output or passing mechanical source graph does not certify scientific interpretation. `run.json` reports answer availability separately from audit scope.

`research-receipt.json`, `audit-receipt.json` and `profile.json` preserve the actual scientific session IDs, CLI version, model/effort, start/end/duration and unique native per-response usage. Cached input and reasoning output are subsets, not added again; unavailable counts remain null. Child-session metadata is checked to verify the no-subagent rule. These receipts exclude the coordinating chat and unrelated account activity.

This is a local **operator CLI** deployment using native Codex authentication and automatic execution approval review with the workspace-write sandbox. Acquisition uses self-contained `coverage_sources.py` public source helpers and selectively reads `OPENALEX_API_KEY` from the operator environment or `.env`; no provider key is handed to a scientific session. It is separate from the legacy DeepSeek container/service: it neither changes that service's model contract nor resets/bypasses its rolling ledger. No model/provider/effort fallback is implemented. The operator entry point requires the Canary checkout and authenticated native Codex CLI; acquisition and extraction have no boundary-service or saved-experiment imports. The restricted legacy worker remains a separate deployment.

Offline contracts use synthetic bodies and native receipt fixtures. They test batching, failure containment, canonical provenance and accounting, not scientific quality or live provider throughput. Real research is documented with its own fresh receipts and independent audit.
