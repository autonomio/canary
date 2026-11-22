# Canary

Canary conducts systematic literature reviews for researchers and agents.
It starts from a question, sets inclusion criteria, and reads the selected
papers in full. Findings link to exact passages that support, qualify, or
contradict them. A separate agent audits the claims against those passages.

The review records what remains open and what evidence could resolve it. It
also preserves relevant observations outside its conclusions for later work.
[Canary on Autonomio Solutions](https://autonom.io/solutions/#canary)

The dashboard, CLI, and MCP tools use the same HTTP API to show the search,
coverage, audit results, and original source context.

## Start here

- **Running research.** Give an agent connected to Canary a question in
  chat, or submit it from the dashboard, CLI, or HTTP API. The service uses
  an authorized Codex account for live research.
  [Installation](docs/INSTALL.md) · [Authentication](docs/AUTH.md)
- **Working on Canary.** Give your coding agent the task in chat. The
  agent sets up the checkout, follows the credential-free preview, and
  reports what it verified. [Contributor guide](GUIDANCE.md)
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

## Continue the investigation

A follow-up review can use saved research state to look for new evidence.
Poise can build and test a software instrument to collect missing evidence.
Galley can use a Canary review to refine a thesis or develop a manuscript.
Canary exports a versioned contribution for Resolvent admission, where
findings can be linked to methods and evidence in a shared world model.
[Continue a review](docs/RESEARCH_BUNDLE.md#continue-from-supplied-research-state) ·
[Resolvent handoff](docs/RESEARCH_BUNDLE.md#resolvent-pilot-and-further-consumers) ·
[Research pipelines](https://autonom.io/solutions/)

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

## License

Canary's software and project documentation are available under the MIT
License. The bundled dashboard fonts use the SIL Open Font License 1.1.
[MIT License](LICENSE) · [Inter notice](scripts/dashboard/assets/Inter-LICENSE.txt) ·
[Finlandica notice](scripts/dashboard/assets/Finlandica-LICENSE.txt)
