# Sources and repository-specific observations

Read-only review on 2026-09-21. No repository code was installed/executed, account authenticated or strategy run. Proposed architecture decisions are recommendations, not features verified in these projects.

## agy-manager is the base

Inspected main at commit [76bfbb562677ce74b37040bf8c03a205cd0456d6](https://github.com/Tiago-0liveira/agy-manager/commit/76bfbb562677ce74b37040bf8c03a205cd0456d6), including README, launcher/profile/diagnostics code, CLI dispatch, packaging and manual integration instructions. It is Python >=3.10, currently with no declared runtime dependencies. `pyproject.toml` declares MIT; the absence of a separate LICENSE entry in the inspected tree should not be described as absence of all license declaration.

Relevant source links:

- [Profiles](https://github.com/Tiago-0liveira/agy-manager/blob/76bfbb562677ce74b37040bf8c03a205cd0456d6/agym/profiles.py): named profiles, platform roots, settings, whole-file metadata writes and removal.
- [Launcher](https://github.com/Tiago-0liveira/agy-manager/blob/76bfbb562677ce74b37040bf8c03a205cd0456d6/agym/launcher.py): host executable resolution, child profile environment, permission-option precedence and process launch.
- [CLI](https://github.com/Tiago-0liveira/agy-manager/blob/76bfbb562677ce74b37040bf8c03a205cd0456d6/agym/cli.py): recognized commands followed by fallback to launching the first argument as a profile.
- [Packaging](https://github.com/Tiago-0liveira/agy-manager/blob/76bfbb562677ce74b37040bf8c03a205cd0456d6/pyproject.toml): currently explicit `packages = ["agym"]`, which needs attention for subpackages.
- [Real-login integration procedure](https://github.com/Tiago-0liveira/agy-manager/blob/76bfbb562677ce74b37040bf8c03a205cd0456d6/docs/manual-integration.md): real auth persistence and concurrency require installation-specific verification, not just unit tests.

Integration decisions derived from the code: use a new command to avoid profile-name collisions; retain original profile storage; add locking for concurrent read/modify/write metadata updates; avoid a process-wide environment change for parallel accounts; override inherited permission-bypass defaults explicitly in scoped council execution; preserve the base install and update package discovery.

The login workaround redirects home-related state and requests file-backed credentials. It is not an OS sandbox or a guarantee that all future CLI builds preserve this behavior. The application must keep those two claims separate.

## Other council applications

[ajfisher/llm-advisors](https://github.com/ajfisher/llm-advisors) supplies a Python CLI/web approach using existing provider CLIs, bounded multi-turn advice and logged artifacts. Its inspected [Antigravity adapter](https://github.com/ajfisher/llm-advisors/blob/main/src/llm_advisors_cli/providers.py) invokes one prompt and returns captured text; it does not itself manage a run-specific persistent researcher session. Use as a source of small adapter/interface ideas, not the core data model for this project.

[yanbrod/council](https://github.com/yanbrod/council) supplies configurable CLI providers, SQLite history, a web/API/MCP surface and a parallel-advice/compilation flow. The inspected [orchestrator](https://github.com/yanbrod/council/blob/main/server/services/orchestrator.js) collects successful advisor answers and then calls a compiler. This is useful interface/orchestration reference, but it lacks the staged research semantics proposed here. Its partial-result behavior should not become silent permission to omit a required reviewer.

[karpathy/llm-council](https://github.com/karpathy/llm-council) demonstrates independent answers, peer ranking and final synthesis through OpenRouter. Its README presents it as an unsupported exploratory project. Useful interaction inspiration; switching to its API-billing architecture would not solve the owner's account-based CLI requirement.

## Official Antigravity surfaces

[Headless mode](https://www.antigravity.google/docs/cli/headless/) documents structured responses, streaming, explicit conversation resumption and non-interactive permission behavior. The adapter should retain conversation identity and verify evidence of required tool execution, not just process success. Check the installed version rather than hard-coding assumptions from a web page.

[Resume](https://www.antigravity.google/docs/cli/commands/resume) documents workspace-keyed 'latest conversation' behavior. This motivates explicit worker-specific IDs under concurrency.

[Authentication](https://antigravity.google/docs/cli-install?hl=en) documents the official interactive login and key-based alternative. Reuse the provider's login flow; do not create a password-collection UI.

[Teamwork](https://antigravity.google/docs/teamwork/) provides a built-in multi-agent option with structured work and verification. It remains a useful comparison or future workflow backend. The documented feature does not by itself establish the project's desired multi-account assignment/control, so it does not eliminate the application concept.

## Framework alternatives

[LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence) separates per-thread checkpoints from longer-lived stores. That distinction informs this design's run-local conversations versus explicitly shared project artifacts. Consider it if nested graphs, interrupts and recovery become the dominant engineering burden; do not add it merely to label the app agentic.

[CrewAI Flows](https://docs.crewai.com/en/concepts/flows) provides event-driven orchestration, state and branching. It is a viable alternative framework, but does not remove this project's account, CLI, artifact-visibility and lifecycle work.

[Microsoft AutoGen](https://github.com/microsoft/autogen) is in maintenance mode and directs new users to [Microsoft Agent Framework](https://learn.microsoft.com/agent-framework). Do not start a new dependency on AutoGen based on older popularity comparisons. Agent Framework is worth considering for a larger provider-integrated system, but is not necessary for this first local staged runner.

Decision: keep core domain contracts independent of any framework; build the initial bounded stages using Python/SQLite and the native CLI. Reevaluate a framework when actual complexity justifies it. Do not create a new general agent framework as a side project within this application.

## Evidence about councils

[Choi, Zhu and Li, Debate or Vote](https://arxiv.org/abs/2508.17536) reports benchmark results in which independent aggregation accounts for much of the gain attributed to multi-agent debate. This does not prove the best workflow for the owner's projects. It supports treating debate/consensus as optional methods and preserving independent submissions and objective verification. More accounts, longer conversations and elaborate personalities are not measures of correctness.
