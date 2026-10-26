# Local text libraries

Use a directory of saved paper texts as the source for a research run. The API owns
indexing, discovery, selected-source import and evidence binding. Dashboard, YAML/CLI
and MCP submissions use the same `ResearchDeclaration`; there is no separate local
research engine or public-source fallback.

## Configure and submit

Allow the source root on the **service computer** before starting the service:

```sh
CANARY_LOCAL_TEXT_ROOTS=/srv/library canary runs serve
canary run docs/examples/local-text.yaml --dry-run
canary run docs/examples/local-text.yaml
```

`CANARY_LOCAL_TEXT_ROOTS` is an explicit allowlist, separated by the platform's path
separator (`:` on macOS/Linux). Both the text directory and optional metadata file
must be inside an allowed root. Container deployments mount this root read-only
and configure the container's paths. Submitted paths are server-side; clients do
not scan or read the library. Compilation checks the declaration without reading
source files; submission checks access before allocating a worker.

In the dashboard, choose **Local text library** in the question composer and enter
those same server paths. Run settings, indexing progress, corpus size, exclusions,
metadata coverage, snapshot hash and lexical search method remain inspectable.
Saved sources show their imported filename, original-byte hash and canonical-text
hash. Source/context/file and bundle/archive routes remain the existing HTTP routes.
MCP's `submit_research` accepts the same local retrieval settings through `spec`.

The example below also works as the `spec` of `POST /api/runs`:

```yaml
version: 1
question: How do formulation conditions affect VLP solubility and assembly in this library?
retrieval:
  corpus: local_text
  local_text:
    directory: /srv/library/papers/fulltext
    metadata: /srv/library/data/library.jsonl
```

The research and audit model profiles, evidence rules and default 50-paper target
are unchanged. A declaration can set a positive `target_papers` and audit budget
using the existing fields. Missing or incomplete publications remain a documented
coverage limitation; the engine must not pad the review count.

## Input and retrieval contract

The adapter recursively indexes nonempty UTF-8 `.txt` files. Hidden paths and
symlink directories are not traversed. Invalid encodings, empty files, symlink files
and unreadable files are excluded with reasons. The corpus is never modified.
An optional JSONL metadata file supplies one object per line:

```json
{"file":"paper.txt","title":"Paper title","doi":"10.1234/example","year":2025,"authors":["Author Name"],"abstract":"Actual source abstract."}
```

`file` is relative to the text directory. Without it, the filename is the DOI
(or OpenAlex ID with `https://openalex.org/` replaced by `oa-`) with each
non-alphanumeric character replaced by `_`, truncated to 120 characters, plus
`.txt`. This supports the Velocin-Library layout directly. DOI URL prefixes are
removed before filename matching. `openalex_id` can also supply an existing
bibliographic link. Missing metadata leaves a filename title and unknown citation
fields. Conflicting records for one filename are reported as ambiguous, and their
metadata is withheld rather than inventing a bibliographic identity.

SQLite FTS5 indexes titles, metadata abstracts and the complete saved text. Queries
use lexical `MATCH` syntax, including `AND`, `OR`, `NOT` and quoted phrases; quote
hyphenated terms. BM25 ranking weights titles and abstracts above body matches.
There is no semantic expansion. Scientist-generated question plans use this
syntax; invalid queries return an explicit error. The existing `pages` option
returns up to 200 candidates per page, with total matches and truncation recorded.
Supplemental searches preserve query IDs and earlier search history.

Local IDs use `L` plus 24 hexadecimal characters; they do not masquerade as OpenAlex
IDs. `local-corpus.json` records the file inventory, original SHA-256 hashes, metadata
hash, exclusions and snapshot identity. The disposable, hidden FTS index stays out
of bundle archives. A candidate excerpt is a discovery aid, distinct from its
metadata abstract and from a reviewed paper.

Selected source import verifies the indexed hash and refuses changed files. It
retains the exact original bytes as `papers/L….source.txt`, normalized canonical
text as `texts/L….txt`, and reproducible source slices in `passages/`. Import never
substitutes a public download. The existing evidence pipeline binds quotations to
these saved copies; later changes to the library cannot change acquired evidence.

## Scope and limits

A local text file is not certification of paper identity or completeness. Text
conversion can lose section structure, tables, figures, layout or version details.
The researcher and independent audit must verify substantive methods/results,
identity and question relevance before counting a publication. `local_text_requires_source_verification`
stays distinct from a verified full paper; suspected preview markers remain visible.
Coverage and absence claims apply only to this indexed corpus and inspected scope.

The file inventory records what was indexed during the scan; it is not an atomic
filesystem snapshot. Only selected unchanged source bytes are preserved in the
portable bundle. Repeating a run repeats settings and rescans external files.
The first discovery builds a per-run disk index; a large library takes additional
startup time and disk space. Cancellation stops the API-owned worker as usual.

The [Velocin acquisition receipt](../validation/local-text-velocin-20261026.json)
records real-corpus retrieval and byte/passage checks separately from scientific
inference. It does not certify the corpus's scientific content or full-text coverage.
