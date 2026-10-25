# Agent instructions

Read AGENTS.md first whenever receiving a message or task.

Use a factual voice and lead with the result. Emphasize only the two or three words that carry the claim. Use the fewest words that coherently convey the matter; user-facing replies must not exceed 20 lines unless the user asks for more. Engineering updates should read like reports to a CTO. Cut social padding.

## Researcher confidence and frontend

The frontend must evolve with the changes in every PR, never as an afterthought. Implement and verify the researcher-facing representation alongside the underlying change.

Every change must ask: "How can this increase researcher confidence through radical transparency?"

## API-first architecture

Canary is API-first. The HTTP API is the canonical boundary for application actions and state.

- Dashboard, CLI, SDKs and agent/MCP tools are clients of the same API. They must not independently launch research or authentication processes, call providers, or read/write run artifacts.
- The API owns authentication, research and audit orchestration, search/acquisition, run control, results, evidence/source access, exports and usage.
- Shared engine functions and workers implement the API internally. Reuse them; do not add HTTP calls between internal functions merely to satisfy this boundary.
- Service startup and package installation are bootstrap operations. Once the service is running, application actions go through its API.
- Define typed requests, responses and errors with each action. Implement and verify the researcher-facing UI, CLI and agent representation alongside that action.
- Follow the canonical migration map in [docs/API_FIRST.md](docs/API_FIRST.md). Existing bypasses remain migration work until implemented and verified; this rule alone does not remove them.

## ACP Governance

You are governed by Agentic Control Plane. Every tool call is logged and policy-checked.

- Bash commands are automatically intercepted by the ACP PreToolUse hook.
- Before every non-Bash tool call, call the acp server's acp_check tool with {tool_name, tool_input}. Proceed only on an allow decision. On deny, stop and report the reason.
- Skip acp_check for acp_check and acp_status themselves.

Activity log: https://cloud.agenticcontrolplane.com/activity
