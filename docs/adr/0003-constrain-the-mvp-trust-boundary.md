# Constrain the MVP trust boundary

The required MVP flow will operate on registered, bundled Fixture Repositories, while Patch
Run execution will depend on a source-neutral Repository Source rather than package fixture
paths. A separate Adapter accepts a local Trusted Repository only when the user explicitly
selects it, provides a validated `patch-run.toml` in its root, and acknowledges that its
Verification code runs with host authority. The contract is loaded before execution and remains
protected in the isolated Run Workspace; the exact Candidate Patch still requires approval.
Path containment limits agent file access but is not a hostile-code sandbox, so implicit
repository discovery and untrusted repositories remain excluded. Sending Repository Source
content to Gemini requires the separate explicit model selection recorded in ADR-0014.
