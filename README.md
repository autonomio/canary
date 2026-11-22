# Canary

Canary is an API-first research service for full-paper literature reviews.
It records the search, reviewed sources, findings, and independent audit.
The dashboard, CLI, and MCP tools use the same HTTP API, including the
path from a finding to the exact source passage behind it.

## Start here

- **Working on Canary.** Give your coding agent the task in chat. The
  agent sets up the checkout, follows the credential-free preview, and
  reports what it verified. [Contributor guide](GUIDANCE.md)
- **Running research.** Install the service, connect an authorized Codex
  account, and start a question from the dashboard, CLI, or HTTP.
  [Installation](docs/INSTALL.md) · [Authentication](docs/AUTH.md)
- **Integrating an application or agent.** Use the live `/openapi.json`
  contract or the `canary mcp` stdio relay. [HTTP and MCP](docs/API.md)

## Commands

On a service configured for live research, `canary ask "Your question"`
returns a run key. Use `canary runs show RUN_KEY` for status and
`canary bundle RUN_KEY` for the recorded findings and evidence. The service
allocates run storage. OpenAlex is the default discovery source.

A question can also be declared in YAML and compiled before submission.
Optional public sources and local text have their own input contracts.
The dashboard shows coverage, claim checks, source context, and run issues.
[Research declarations](docs/RESEARCH_BUNDLE.md) ·
[Optional sources](docs/OPTIONAL_SOURCES.md) · [Local text](docs/LOCAL_TEXT.md) ·
[Dashboard](docs/DASHBOARD.md)

## Development and evidence

The contributor guide starts with the locked checkout-local environment and
a synthetic API and MCP test. It also walks through a local dashboard and
compile-only request. The separate installation receipt records an actual
research run and its partial audit.
[Contributor guide](GUIDANCE.md) · [Deployment evidence](docs/DEPLOYMENT_PROOF.md)

## Retained broker workflows

The root `.env.example` documents the Muse request broker setup.
The operator runbook covers review, analysis, cycles, exports, and
containment checks for the retained broker and its worker image. The token
boundary documents the broker's fixed model and accounting rules.
[Operator runbook](docs/RUNBOOK.md) · [Token boundary](docs/TOKEN_BOUNDARY.md) ·
[Compatibility commands](docs/RUN_LIFECYCLE.md#retained-compatibility-workflows-and-inputs)

## Historical material

The pre-migration source and recovery archive are in Git at
`pre-canary-20260926`. [Historical handoff](docs/HANDOFF.md) ·
[Naming validation](docs/VALIDATION.md)
