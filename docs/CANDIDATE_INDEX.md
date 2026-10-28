# Candidate metadata and memory

Each question's worker stores discovered work records in `.candidate-index.sqlite`.
A search page commits new records and query links in one transaction. First-seen
metadata, discovery rank and query-link order are preserved; overlapping searches
add provenance without replacing the first record. Titles, abstracts and acquisition
metadata are not pruned to save memory.

`candidates.jsonl` and `titles.tsv` remain the agent-facing snapshots. They are
streamed from the index after each committed page, without building a complete
Python collection or serializing a corpus-sized JSON string. Acquisition reads
only the selected IDs. Progress exports fetch candidate details only for known
sources while retaining the complete discovered-candidate count.

An older `works.json` is imported once, one JSON record at a time. The old file
is preserved. A failed import never publishes a partial index. New indexed
workspaces do not create or rewrite `works.json`.

The index is temporary working state, excluded from the bundle's artifact
inventory. Raw search receipts and candidate snapshots retain discovery provenance.
`search-progress.json` and the API discovery response expose typed
`candidate_metadata` (`storage`, `records`, `acquisition_lookup`). The dashboard's
selection details state that the complete candidate net was available for
relevance assessment and only selected records are loaded for paper acquisition.

This changes metadata representation only. Search requests, relevance inputs,
source acquisition concurrency, scientific models/effort and research scope remain
the same. CPU scheduling and shared Codex execution are separate work.

## Measured metadata replay

The [recorded Linux arm64 benchmark](../validation/candidate-index-profile-20261028.json)
replays the same 50 saved search pages (6,533 unique records) in fresh subprocesses
under a one-vCPU Docker budget, with networking disabled:

| Operation | Previous peak RSS | Indexed peak RSS | Previous time | Indexed time |
| --- | ---: | ---: | ---: | ---: |
| Page-by-page metadata ingestion and candidate snapshots | 1,116.76 MiB | 84.38 MiB | 85.179 s | 22.533 s |
| Loading 82 selected acquisition records | 863.87 MiB | 33.63 MiB | 2.279 s | 0.019 s |

Full work-record and candidate-view semantic hashes match; candidate snapshots
and titles match byte for byte, and all selected records match. These numbers
measure metadata handling,
not model inference, paper extraction, total research memory or concurrent-question
capacity. Wall time and `cpu_seconds` isolate the metadata operations.
`total_subprocess_cpu_seconds` also includes imports and digest checks. No model
or provider request was made.
