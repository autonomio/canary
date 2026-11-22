# Contributing to Canary

Start with a request in chat.

> Set up Canary in a clean checkout. Verify that its dashboard loads and that
> the example research declaration compiles. Tell me what you observed.

The agent reads [AGENTS.md](AGENTS.md), checks the current revision, runs the
steps, and reports the commands and outcomes in chat. Apply the same workflow
to the first contribution, following the code path that the request touches.

## Set up the checkout

Use Git and uv. Run this block from a parent directory where `canary` can be
a new checkout. The locked environment installs into that checkout's `.venv`.
Use Python 3.12 to match CI's main smoke job.

```sh
git clone https://github.com/autonomio/canary.git
cd canary
uv sync --locked --python 3.12
uv run --no-sync pytest -q tests/test_api_contract.py
```

If the agent sandbox blocks writes to the default uv cache, run
`export UV_CACHE_DIR="$PWD/out/uv-cache"` from the checkout root and retry
the sync. Keep that setting in later shells for this checkout.

The test exercises the real HTTP and MCP clients against synthetic runs,
including retrieval of the original source behind a claim. Dependency setup
can use package registries. The fixtures need permission to open loopback sockets.
For the portable suite, run `uv run --no-sync pytest -q`. Pytest's default
selection excludes live and Linux containment tests.

## Try the service

Follow the [dashboard quickstart](docs/DASHBOARD.md#quickstart) from this
checkout. Start the preview in its own shell or terminal session, use its
printed URL for the checks, and stop that session when done. Report what
**Questions** and **Menu → Service** show, the resolved audit settings, and
whether the preview's run list is empty. The dry run compiles via HTTP.

## Find the code for a change

| Area | Entry point |
| --- | --- |
| Dashboard | [HTML and JavaScript](scripts/dashboard/index.html) |
| CLI and agent tools | [CLI dispatch](src/canary/cli.py), [command client](src/canary/command_client.py), [MCP relay](src/canary/mcp_server.py) |
| HTTP boundary | [Request handler](src/canary/dashboard.py), [OpenAPI contract](src/canary/api_contract.py) |
| Research and evidence | [Declaration compiler](src/canary/research_spec.py), [research runtime](src/canary/coverage_runtime.py), [bundle export](src/canary/coverage_export.py) |

For an API change, trace the request from its clients through the handler and
verify the relevant response in the dashboard, CLI, and agent tools. For
dashboard changes, run `python3 scripts/check_dashboard_js.py` with Node
available and exercise the affected view. Report the tested revision, the
checks that ran, and any gaps. Use public or synthetic material for fixtures.
Live operation and its evidence are documented separately.
[API-first map](docs/API_FIRST.md) · [Deployment evidence](docs/DEPLOYMENT_PROOF.md)

## Downstream compatibility

- Galley submits systematic reviews over HTTP and reads the returned bundle
  for manuscript evidence. It also imports `canary.coverage_runtime.discover`
  for citation search and reads the first-page receipts. Its Astra/ultra,
  600-second review declaration compiles on Canary `930a909`. Current Canary
  requires Sol/xhigh with an unlimited audit. Upgrading that Galley integration
  requires coordinating the audit-profile change.
  [Review client](https://github.com/autonomio/galley/blob/3c3dd730d50e45814ef541c54be113a83addf55c/src/engine/literature.ts#L12-L74) ·
  [Discovery bridge](https://github.com/autonomio/galley/blob/3c3dd730d50e45814ef541c54be113a83addf55c/src/engine/assets/canary_search.py#L41-L74)
- A pinned Resolvent consumer uses the HTTP run lifecycle, preserves the
  compact connector v5 export bytes, and expands them with the installed
  `canary.bundle_encoding.expand`. Its offline reader and handoff fixtures
  passed on Canary `35c0242`
  and `3027f46`. Check both the HTTP contract and compact export round-trip when
  changing this path. [Bundle contract](docs/RESEARCH_BUNDLE.md#apiclient-contract)

For retained broker or revision work, use the matching runbook. Public
revision entry points refuse on every host. Exercise revision logic through
the hermetic suite and keep the broker's model and accounting rules intact.
[Operator runbook](docs/RUNBOOK.md) · [Trust boundary](docs/TRUST_BOUNDARY.md) ·
[Token boundary](docs/TOKEN_BOUNDARY.md)
