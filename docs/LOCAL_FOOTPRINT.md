# Local research footprint

Start the local service and dashboard with `canary runs serve`. The dashboard has
no provider login or sign-out controls. Authenticate the service separately with
`canary auth login`; the CLI still uses the same authentication API and credential
context as research workers.

The service indexes existing runs at startup under `CANARY_DATA_DIR/out` and
`CANARY_DATA_DIR/container-out`. Add historical locations with
`CANARY_RESEARCH_ROOTS`, using the platform path separator (`:` on macOS/Linux):

```sh
CANARY_DATA_DIR="$HOME/.local/share/canary" \
CANARY_RESEARCH_ROOTS="/path/to/old-checkout/out:/path/to/other/container-out" \
canary runs serve
```

These are server-side locations. They are not client upload paths. The index
includes native and historical runs, incomplete runs and failures, without copying
or editing their scientific files. It preserves existing names, keys, timestamps,
worker ownership and compiled settings. Same-named run folders receive distinct,
stable keys. Symbolic-link subdirectories and source/virtual-environment directories
are not traversed. No whole-computer scan occurs.

The menu's **Local footprint** shows configured roots, missing folders, scan times
and errors. **Refresh local index** adds newly discovered runs. Missing folders
remain registered and visibly marked. Imported active manifests without an owned
live worker show **Activity unverified**; their original status remains in the API
record. They are not counted as confirmed live research.

| Client | Inspect | Refresh index |
| --- | --- | --- |
| HTTP | `GET /api/footprint` | `POST /api/footprint/refresh` with `{}` |
| CLI | `canary runs footprint` | `canary runs discover` |
| MCP | `canary_footprint` | `canary_refresh_footprint` |

The existing run, findings, source, usage and log endpoints provide each indexed
run's retained research. Legacy runs remain explicitly unversioned; indexing does
not invent compiled YAML, source provenance, unknown token counts or completion.
The original source directories must remain available. To transport a versioned
run, use its API bundle archive.
