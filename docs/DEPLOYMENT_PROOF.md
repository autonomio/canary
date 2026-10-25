# Fresh installation proof — 25 October 2026

A fresh virtual environment installed the noneditable wheel with the locked runtime
dependencies and started the service outside every source checkout. Native Codex
0.162.0 used the existing authenticated file-backed ChatGPT context. Only the
OpenAlex key was supplied to source acquisition; no earlier paper or answer assets
were reused. The service compiled and submitted the saved YAML through HTTP.

Question: **How does infill density affect tensile strength of fused-filament
printed PLA, and which printing conditions limit comparisons between studies?**

| Stage | Seconds | Input | Cached input (subset) | Output | Reasoning output (subset) |
| --- | ---: | ---: | ---: | ---: | ---: |
| Sol 6.1 xhigh research | 1,551.676 | 12,132,293 | 11,891,072 | 65,984 | 12,810 |
| Astra 6 ultra audit | 600.080 | 2,206,159 | 2,039,680 | 14,625 | 4,707 |

Wall time was **2,213.630 seconds (36m 54s)**. The difference from stage totals is
orchestration/finalization, not additional uncounted model work. Native receipts
reported zero cache-write input in both stages. Cached input and reasoning output
are subsets, never extra tokens. Both scientific threads verified no subagents.
The CLI recorded an initial rate-limit retry; its time remains in the measurement.
This is evidence of operation, not a claim that a 50-paper review takes a few minutes.

The run retrieved 55 papers, recorded 50 substantive reviews (30 core, 20 supporting),
and produced 16 claims with no mechanical binding issues. Astra persisted checks
for all 16 claims but reached its 600-second budget before completing broader
source-role/review-count verification. The audit remains **partial**, with its
record issues retained; structural completion is not scientific certification.

The worker survived service replacement with the same process identity and
remained controllable. A separate real native-thread cancellation took 0.336 seconds
and preserved a finished cancelled bundle. The installed stdio MCP client read the
50-paper run through the same API. HTTP alone reconstructed claim C02 through
E01.6 to W3128926079-p0035, returned the surrounding PDF page and retrieved the
771,840-byte original with its recorded SHA-256. See the [operational receipt](../validation/clean-install-20261025.json).

The first attempt exposed conflicting native CLI flags before any model thread.
That failed bundle remains intact. The corrected invocation uses the approval
preset's workspace-write sandbox; the dashboard exposes failed-start diagnostics
and the public operational progress journal. Raw private native events are not a
researcher log source.

The Linux service-image check additionally executes a real Codex sandbox command
and calls the local research API from inside it. Docker's default outer security
profile blocked namespace creation; the explicit Compose runtime options allow
Codex's inner bubblewrap/seccomp sandbox. No inference or credentials are used by
this container fixture. See [installation](INSTALL.md).
