# Codex authentication

The loopback API owns one native Codex authentication process. The CLI is a client of that API; the local dashboard has no provider-login controls. Authentication does not establish access to a particular model or prove research readiness.

## Operator workflow

Install the Codex CLI on the service host and make `codex` available on its `PATH` (verified with 0.162.0). Connect:

```sh
canary connect
# Headless host:
canary connect --device
```

This starts the local service if it is stopped, reuses an existing native login, and
opens native sign-in only when signed out. Repeating it does not start another
service or replace a signed-in account. It prints the API origin, CLI version,
authentication method, resolved credential store and service-host execution boundary.
Sign-in does not certify model access; the first inference request establishes that.
It performs no research or test inference. Missing Codex requires installation;
Canary does not install it silently. Every connect command accepts `--port`.
Service bootstrap retains the caller's working directory, so relative data and
credential settings resolve exactly as they do for an explicitly started service.

The lower-level commands remain available:

```sh
canary runs serve --port 8789
# In another terminal:
canary auth login
canary auth status --json
canary auth logout
```

For a headless host, use `canary auth login --device`. Complete the returned URL/code in a browser; device login must be enabled for the ChatGPT account/workspace. Browser login uses the native Codex callback on the service host. Ctrl-C cancels a pending CLI login. Every auth command accepts `--port`; `CANARY_RUNS_PORT` supplies its default.

The dashboard opens locally without a login flow. Use the CLI for provider status, browser/device login, cancellation and sign-out. Challenges are transient; completed/cancelled logins do not retain their URL/code.

## Shared credential context

Set `CODEX_HOME` **on the service** to a persistent directory before startup, or use the default `~/.codex`. The service captures this location once. Native `config/read` resolves Codex's existing credential store; the service pins the same home, executable and store for research and audit workers, including their isolated scientific configuration. Both file and OS-keyring logins can be reused. Codex owns storage, refresh and logout; Canary does not read, copy, return or log tokens. `CANARY_CODEX_CREDENTIALS_STORE=file|keyring|auto` on the service explicitly overrides the native setting when needed for deployment. Ephemeral credentials cannot be shared with separate workers and produce an actionable unavailable status.

`CANARY_API_URL` can select an existing private remote service. Connect never starts
a local worker as a substitute for that service. Codex and its credentials must be
on the Canary host; use `--device` for remote/headless sign-in. Each instance uses
one shared account. Attaching a laptop CLI to a remote instance is not implemented.

Stopping the service ends its pending login but does not log out the account. After restart, status comes from Codex's persistent credentials. Pending login IDs do not survive a service restart: start a new login. Signing out affects this shared Codex context, including other local Codex clients using it; it does not cancel already running research.

This is an owner-operated service bound to `127.0.0.1`, not a multi-user hosted login. Auth endpoints reject foreign Host/Origin and cross-site browser requests, require JSON mutations, and send `Cache-Control: no-store`. Keep the API private; provider authentication is separate from access to Canary. Explicit private-host and allowed-origin client configuration is documented in [the API contract](API.md); authentication remains same-origin.

## HTTP contract

Requests and responses are typed in [`codex_auth.py`](../src/canary/codex_auth.py). No endpoint accepts credentials or a client-side credential path.

`GET /api/connection` returns a typed, read-only connection receipt, also exposed
as the `canary_connection` MCP tool and in the dashboard's Service menu. It never
returns a pending login challenge or starts login/inference. The dashboard remains
login-free; use `canary connect` for sign-in. Its status is refreshed on menu open.

| Method | Path | Body / response |
| --- | --- | --- |
| GET | `/api/auth/status` | `AuthStatus` |
| POST | `/api/auth/login` | `{"method":"browser"}` or `{"method":"device"}`; omitted method defaults to browser. Returns `Login`. Reuses the current pending flow. |
| GET | `/api/auth/login/{id}` | `Login` progress; unknown/restarted IDs return 404. |
| POST | `/api/auth/login/{id}/cancel` | `{}` → terminal `Login` |
| POST | `/api/auth/logout` | `{}` → `AuthStatus` |

`AuthStatus` contains `available`, `authenticated`, `state` (`signed_in`, `signed_out`, `unavailable`), `mode` (`chatgpt`, `apiKey`, or null), `login` (or null), and `error` (or null). Unavailable status is a successful status read (HTTP 200), not a false signed-out result. It does not promise model availability.

`Login` contains `id`, `method` (`browser` or `device`), `state` (`pending`, `complete`, `failed`, `cancelled`), `url`, `user_code`, and `error`. Only a pending login has a URL/code. Completion is reported by Codex, not inferred from opening a browser. Poll progress/status while pending; no scientific session is started.

Errors use `{"error":"safe message","code":"machine_code"}` with HTTP 400/415 (invalid request), 403 (foreign origin), 404 (unknown login/action), 502 (native rejection) or 503 (missing/unavailable Codex). Raw provider error text is withheld. Clients must distinguish unavailable service, unavailable CLI, signed out and pending login.

The adapter uses native stdio JSON-RPC `initialize`, `account/read`, `account/login/start`, `account/login/completed`, `account/login/cancel`, and `account/logout`. Account actions do not create research threads. The same service-owned transport
also supports [isolated scientific execution](SHARED_CODEX.md); request IDs and
thread IDs keep account replies separate from scientific events. See the [native protocol](https://learn.chatgpt.com/docs/app-server) and [Codex authentication](https://learn.chatgpt.com/docs/auth).
