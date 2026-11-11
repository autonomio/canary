# Optional public research sources

OpenAlex remains the default. A submission without additional sources follows the
existing paper discovery, acquisition, research and independent-audit path.

```sh
canary ask "Your question" --add-source clinicaltrials --add-source osf
canary ask "Your question" --add-source epo
```

Repeat `--add-source` to combine providers. CLI, dashboard and MCP are clients of
the same API. JSON and YAML declarations use the same compiler:

```yaml
version: 1
question: Your question
retrieval:
  additional_sources: [clinicaltrials, epo, osf]
```

`additional_sources` accepts only these three names, without duplicates. Omission
or an empty list preserves the existing resolved default settings and digest.
Additional public sources cannot be combined with `corpus: local_text`. Compilation
performs no network or credential checks. The service rejects an EPO-enabled run
before allocating its worker when its credentials are missing.

## Provider scope

| Name | Search | Acquired source | Limits |
| --- | --- | --- | --- |
| `clinicaltrials` | ClinicalTrials.gov v2 `query.term`; cursor pagination | Complete public JSON record, including posted results when available | Registry coverage only; aggregate, not participant-level data. Missing posted results do not imply a null finding. |
| `epo` | OPS bibliographic CQL; paged ranges | Available description/claims XML in a response snapshot | Requires OPS credentials. Free up to 4 GB/week. No description/claims search; 2,000 hits per query, requiring query partitioning. Full-text availability varies by authority. |
| `osf` | OSF Trove metadata search restricted to public Registrations | Registration responses and available registration-form schema in a response snapshot | Study plans, not completed findings. Linked files, datasets and articles are not automatically read. Registration dates, revisions and withdrawals need inspection. |

Configure `EPO_OPS_KEY` and `EPO_OPS_SECRET` in the API service environment or its
data-directory `.env`. These credentials are not declaration fields and are never
included in requests from research clients, source receipts or exported payloads.
OPS account approval and app registration are operator prerequisites. There is no
paid-provider or scraped-site fallback. An unavailable OPS section is recorded; a
patent with neither readable description nor claims contributes no body evidence.

Provider references: [ClinicalTrials.gov API](https://clinicaltrials.gov/data-api/api),
[OPS](https://www.epo.org/en/searching-for-patents/data/web-services/ops),
[OSF Trove API](https://share.osf.io/trove/docs/openapi.html),
[OSF project transition and continuing public read access](https://help.osf.io/article/744-osf-projects-api-impact).

## Research, provenance and receiving semantics

The scientific plan supplies a distinct query `source` and its appropriate syntax.
The initial plan must include OpenAlex and every enabled provider. Supplemental
queries retain original IDs, text and providers. Each provider resumes its own
cursor. An OpenAlex pause does not suppress the other providers. Failed searches,
truncated enumeration and provider caps remain incomplete; they are not zero-hit
or complete-coverage assertions. Only OpenAlex pages count toward OpenAlex usage.
Search receipts retain public throttling, quota and rejection headers even on
provider errors; authentication headers, cookies and error bodies are excluded.
OPS search's specific `404 / SERVER.EntityNotFound / No results found` response
denotes an empty result. That fault is a completed zero-hit page with its original
status and XML snapshot recorded; other 404 responses remain failures.

`additional-sources.json` records source names, query IDs, completion, candidate and
retrieved counts, failures and scope limitations. The API and dashboard expose the
same status. References carry `adapter`, `source_kind`, `source_metadata` and
`record_url`; metadata retains native identifiers, trial/publication dates,
related-publication IDs, patent family IDs or registration status, as supplied.

Acquisition, automatic research completion and explicit finalization reject
incomplete source records with the source ID and missing or invalid fields.
Agent-generated code verification outputs are not admissible sources. Existing malformed records remain exportable: the source
inspector, agent catalog and exported metadata expose `source_record_issues`, and
the Markdown answer lists them under the reference. Missing record URLs remain
unknown; no bibliographic URL is invented for these records.

Registry JSON is deterministically formatted for reading. OSF and OPS snapshots
retain the exact UTF-8 provider responses plus each response's URL and hash;
canonical text has a separate hash. Immutable passage offsets address that exact
canonical text. A snapshot hash identifies the saved response envelope, not an
invented hash of the patent publication. Successful retrieval does not certify
scientific interpretation or completeness of externally linked materials.

Additional documents have their own review count. They do not satisfy the existing
paper target. Research and independent-audit instructions distinguish protocol
intentions, posted trial results, OSF plans and patent disclosures, and require
study/family relationships to be considered rather than assuming independence.

Resolvent native records keep its existing `web` kind for registry records and
`publication` for patent disclosures. Exact provider type/identity/version metadata
is preserved inside each source locator and `method_metadata.source_metadata`;
requested-source coverage remains in `method_metadata.additional_sources`. Handoff
instructions explicitly preserve the plan/result/disclosure distinction. Candidate
uncertainty and independence retain the existing conservative admission policy.
No receiving protocol or plugin upgrade is required: this adds metadata to existing
supported fields without inventing new native kinds or dropping the distinctions.

The receiver verifier supports historical full-workspace archives and current
compact contributions. Compact verification checks transported exact excerpts,
source kinds, locators and recorded hashes; it does not reconstruct released full
originals. Its disposable receiver performs actual dry run, apply, closure compilation
and idempotent retry. Supplied admission decisions remain separate from transport
checks; no production world-model write is made by export or the verifier.

The [recorded live validation](../validation/optional-sources-20261102.json) covers
50 reviewed publications, seven ClinicalTrials.gov records and four OSF registrations,
followed by actual disposable Resolvent admission. The 600-second independent audit
checked all six claims but retained a minor timing correction and incomplete selection
review; those limits travel with the payload.

The subsequent [all-source live validation](../validation/optional-sources-epo-20261102.json)
also covers real OPS OAuth, paginated search and three patents with both descriptions
and claims. It reviewed 50 publications and 13 additional records, verified their
body hashes and all 38 additional-source evidence offsets, then passed actual
Resolvent dry run, apply, closure compilation and retry with all 80 retained excerpts.
The 600-second audit supported five claims, including all three additional-source
claims; C1 and selection review remained unchecked. Bounded receiver decisions
admitted the registry, registration-plan and patent-disclosure statements and
deferred the other candidates. Unfinished OpenAlex searches and audit limits
remain attached to the contribution.
Additional live backend contract checks verified OPS empty-search handling and
two 100-record OSF continuation pages; the receipt distinguishes these component
checks from the complete API-owned research run.
