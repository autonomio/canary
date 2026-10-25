# Install the research service

Canary's current research pipeline uses native Codex with Sol 6.1 extra-high and
Astra 6 ultra, one agent per stage. Install Codex on the service host using the
[official npm distribution](https://developers.openai.com/cookbook/examples/codex/build_iterative_repair_loops_with_codex):

```sh
npm install -g @openai/codex@0.162.0
python -m pip install /path/to/canary-0.1.0-py3-none-any.whl
```

Use Python 3.10+ on Linux or macOS. Install Poppler (`poppler-utils` on Debian/Ubuntu,
`poppler` with Homebrew) for layout-preserving PDF extraction. Without it, the
existing pypdf fallback remains available and the service reports that distinction.
Git, curl and ripgrep should be on the worker's PATH for source inspection.
The wheel includes the dashboard, fonts, launcher and dependency-lock fingerprint;
no source checkout is needed to start the service or use its native research tools.

```sh
export OPENALEX_API_KEY=your-openalex-key
canary runs serve
# Another terminal:
canary runs status
canary auth login
canary auth status --json
```

Open `http://127.0.0.1:8789`. Use `canary runs serve --no-dashboard` for API-only
operation. `GET /api/status` and `canary runs status` expose runtime, storage and
extraction status; authentication is separate at `/api/auth/status`. Neither proves
account access to the fixed research/audit models. No vendor API key alone replaces
native Codex authentication. See [authentication](AUTH.md) and
[compiled research declarations](RESEARCH_BUNDLE.md).

## Persistent state

Set `CANARY_DATA_DIR` before service startup. Its default is
`$XDG_DATA_HOME/canary`, or `~/.local/share/canary`. Registry, staged input, run
directories and OpenAlex usage live under this root, independently of the package
installation and current working directory. `CODEX_HOME` remains the separate
credential/session store, defaulting to `~/.codex`. Back up these two roots according
to their different contents; credentials are never included in research bundles.

To keep an existing checkout's registry and paths, set `CANARY_DATA_DIR` to that
checkout's root before starting the updated service. The service indexes configured
research roots without moving or rewriting scientific files. See [local footprint](LOCAL_FOOTPRINT.md).

## Container

```sh
OPENALEX_API_KEY=your-openalex-key docker compose up --build -d
docker compose exec canary canary auth login --device
docker compose exec canary canary runs status
```

`Dockerfile.service` installs the wheel, Codex 0.162.0, Poppler and inspection tools.
Compose publishes only `127.0.0.1:8789`; `/data` and `/auth` have separate persistent
volumes. The service binds all container interfaces to support this port mapping.
Do not expose its unauthenticated owner API publicly. Device login requires account
support and a human completing the challenge. `docker compose down` retains volumes;
`down -v` deletes them. For headless operation, override the command with
`runs serve --host 0.0.0.0 --no-dashboard`.

The older `Dockerfile` remains the legacy contained-worker image for retained
experiments; it is not the current research-service deployment. API-owned native
cancellation and OpenAPI/MCP are documented in [the delivery map](API_FIRST.md).

## Installation evidence

CI builds a wheel, installs it with locked runtime dependencies into a fresh virtual
environment, and starts it outside the checkout. It checks API, compilation, browser
assets, worker tools and separation of state/resources. The service-container check
exercises startup, native Codex discovery, signed-out auth, extraction and persistent
data/auth roots across replacement containers. These are operational fixtures, not
a live scientific run or model-access certification.

The dashboard is local and has no provider-login controls. Authenticate with the
API-backed CLI. See [local research footprint](LOCAL_FOOTPRINT.md) to include old
research directories without moving or rewriting their files.
