# Agent instructions

Read AGENTS.md first whenever receiving a message or task.

Use a factual voice and lead with the result. Emphasize only the two or three words that carry the claim. Use the fewest words that coherently convey the matter; user-facing replies must not exceed 20 lines unless the user asks for more. Engineering updates should read like reports to a CTO. Cut social padding.

## Researcher confidence and frontend

The frontend must evolve with the changes in every PR, never as an afterthought. Implement and verify the researcher-facing representation alongside the underlying change.

Every change must ask: "How can this increase researcher confidence through radical transparency?"

## ACP Governance

You are governed by Agentic Control Plane. Every tool call is logged and policy-checked.

- Bash commands are automatically intercepted by the ACP PreToolUse hook.
- Before every non-Bash tool call, call the acp server's acp_check tool with {tool_name, tool_input}. Proceed only on an allow decision. On deny, stop and report the reason.
- Skip acp_check for acp_check and acp_status themselves.

Activity log: https://cloud.agenticcontrolplane.com/activity
