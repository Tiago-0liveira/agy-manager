# AGYM Council handoff packet

This packet specifies a general-purpose, local AI team application built on your existing agy-manager. It contains a product/engineering blueprint, proposed contracts, example configurations, acceptance scenarios and an exact implementation prompt. It does not contain a working council engine or change your GitHub repository.

Read BLUEPRINT.md first. To hand the work to another LLM, supply this entire packet plus access to your agy-manager checkout and paste BUILD_HANDOFF.md. Ask it to deliver tested vertical slices in the order described. Current source compatibility was reviewed against commit `76bfbb562677ce74b37040bf8c03a205cd0456d6`; the builder must inspect newer changes before editing.

Files:

| File | Purpose |
|---|---|
| BLUEPRINT.md | Product behavior, UI structure, architecture, persistence, account integration and build sequence |
| CONTRACTS.md | Data objects, provider interface, workflow semantics, state transitions and API surface |
| BUILD_HANDOFF.md | Exact prompt for the implementation LLM |
| ACCEPTANCE.md | Concrete functional, recovery, isolation and compatibility tests |
| SOURCES_AND_REPO_REVIEW.md | Primary sources and code-specific integration findings |
| examples/quick_council.json | General opinions, critique and synthesis |
| examples/review_and_revise.json | Writer/reviewer collaboration |
| examples/research_and_design.json | General investigation and independent design comparison |
| validate_examples.py | Lightweight packet consistency checks; not a production engine |
| VALIDATION.json | Results from checking the packet's examples |
| MANIFEST.json | File hashes for this handoff version |

Examples use illustrative profile names and `<choose-discovered-model>` placeholders. They require account/model/input binding before execution and are explicitly marked drafts. The validator permits those draft bindings so the packet remains portable; the production launch validator must reject unresolved bindings.

The architecture deliberately separates account access from agent personality/task and from workflow rules. It places no four-account cap on the product. Parallelism is configurable and bounded. Specialized investigations can become presets without adding domain-specific logic to the core application.

The source review did not authenticate accounts or exercise the actual Antigravity runtime. Real-account isolation, CLI protocol compatibility and Windows process cleanup remain implementation/integration tests, as stated in ACCEPTANCE.md.
