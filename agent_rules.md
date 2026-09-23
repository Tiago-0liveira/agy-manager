You are implementing one narrowly scoped part of AGYM's new orchestration system.

Rules:
- The current repository is the source of truth. Adapt to existing code instead of forcing assumptions.
- Completely ignore `agym/council/` and `tests/council/`. Do not inspect, reuse, depend on, or modify them.
- Stay strictly within the files and responsibility assigned in your specific task.
- Do not modify shared/unowned files just to make integration easier. If you discover a required cross-module change, document it instead.
- Reuse existing AGYM profile, launcher, quota, cache, and authentication logic where appropriate; do not build duplicate systems.
- Follow the frozen orchestration contracts exactly. Do not invent competing types, enums, interfaces, or architecture unless the task explicitly requires a contract change.
- Keep coordinator/model reasoning separate from deterministic AGYM execution and safety rules.
- Tests must use zero real Gemini/Antigravity quota unless the task explicitly says it is a real-account smoke test.
- Prefer small, focused, standard-library Python changes over new dependencies or abstractions.
- Add tests for everything you implement and run the relevant test suite before finishing.
- Do not implement unrelated improvements or refactors.
- When finished, report: files changed, tests run/results, important assumptions, and any integration issues another agent must know about.
