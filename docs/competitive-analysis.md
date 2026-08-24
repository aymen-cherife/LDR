# Agent debugging landscape — August 2026

This comparison is intentionally narrow: tools that give repository-level coding agents runtime evidence while fixing executable code.

## Position

LDR is not the first agent debugger and is not currently the most capable one. Its useful wedge is automatic, bounded post-mortem evidence from a failing Python test, followed by one selective state-inspection pass and a persistent behavioral diff across edits.

| Project | Ahead of LDR | Behind or exposed | LDR decision |
| --- | --- | --- | --- |
| [ADI 2.6](https://github.com/GhabiX/ADI) | Python 3.9+, frame listing, lifetime traces, conditional selection, exec-in-frame, call-tree navigation, agent skill, npm distribution, published SWE-bench gains | Requires the agent to nominate a function before it gets evidence; much larger tracing surface; npm/Node launcher; repository exposes no license; its `diff` compares selected calls rather than persistent invocation snapshots | Do not clone its command set. Lead with automatic failure localization, smaller trusted output, durable before/after evidence and no arbitrary in-frame execution. |
| [AIDB](https://github.com/ai-debugger-inc/aidb) | Python, JavaScript/TypeScript and Java through standard DAP adapters; MCP integration; Python 3.10+; mature breakpoint semantics | Low-level interactive sessions cost agent turns; adapter/runtime setup is heavier; no published repair benchmark in the repository; remote attach remains [open](https://github.com/ai-debugger-inc/aidb/issues/10) | Stay terminal-first until CLI evidence proves value. MCP and language breadth are later distribution work, not 0.5 work. |
| [debug-gym](https://github.com/microsoft/debug-gym) | Real agent evaluation framework, Docker/Kubernetes backends, several benchmarks and model providers | It is a research harness rather than a drop-in project tool; pdb is Linux/PTTY-sensitive; Python 3.12 requirement; open gaps include [view-size bounds](https://github.com/microsoft/debug-gym/issues/320), run-facing [token accounting](https://github.com/microsoft/debug-gym/issues/304), [skill packaging](https://github.com/microsoft/debug-gym/issues/357), and [standard SWE-bench reproduction](https://github.com/microsoft/debug-gym/issues/210) | Reuse its evaluation lessons, not its platform scope. LDR output must remain bounded and the harness must record cost from day one. |
| [Debug2Fix](https://arxiv.org/abs/2602.18571) | Demonstrates a dedicated debugging subagent and reports Python/Java repair gains with controlled ablations | The paper reports high added token/step cost and very uneven natural debugger adoption; Claude Sonnet 4.5 invoked the Python debug subagent on only about 8% of tasks in its unforced setting | Ship explicit usage instructions and measure call rate. Do not assume installation means use. |

## Current scorecard

| Capability | LDR 0.5 | ADI | AIDB | debug-gym |
| --- | ---: | ---: | ---: | ---: |
| Automatic failing-path packet | Strong | Weak | Weak | Medium |
| Selective local-state history | Medium | Strong | Strong, interactive | Strong, interactive |
| Persistent pre/post-edit behavioral diff | Strong | Limited | No | No |
| Bounded agent-readable output | Strong | Medium | Tool-dependent | Open issue |
| Multi-language | No | No | Strong | Python debugger only |
| Python below 3.12 | No | Strong | Strong | No |
| Published agent repair evaluation | Not yet | Strong | None found | Research baselines |
| Install/distribution maturity | Source only | npm | PyPI/MCP | PyPI/framework |

## Upstream contribution candidates

Do not open broad competitive issues or promotional pull requests. Two existing debug-gym issues are credible engineering contributions after LDR 0.5 is stable:

1. [#320 — Size limit for view](https://github.com/microsoft/debug-gym/issues/320): add byte/line bounds plus offset/limit metadata. LDR's head/tail omission contract is directly reusable as a design reference. Small, testable contribution.
2. [#304 — Token usage stats](https://github.com/microsoft/debug-gym/issues/304): current `BaseAgent.run()` already accumulates and logs prompt/response tokens each step, so the remaining work is exposing those totals consistently through `run.py` and stored trajectory summaries. Confirm the expected output contract with maintainers before coding; a second hidden counter would be the wrong fix.

Avoid taking AIDB's remote-debugging issue merely for visibility; it requires cross-adapter expertise and does not advance LDR's wedge. Avoid filing ADI feature requests until its licensing status is clarified.

## Go/no-go evidence

Continue beyond 0.5 only if the paired evaluation shows at least two additional resolutions in eight tasks, or at least 25% lower investigation cost without a lower solve rate. Until then, LDR is behind ADI in proof and feature depth, regardless of internal test quality.
