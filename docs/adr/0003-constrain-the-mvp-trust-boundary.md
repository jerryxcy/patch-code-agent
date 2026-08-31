# Constrain the MVP trust boundary

The required MVP flow will operate on registered, bundled Fixture Repositories, while Patch
Run execution will depend on a source-neutral Repository Source rather than package fixture
paths. A separate Adapter accepts a local Trusted Repository only when the user explicitly
selects it, supplies an external validated Patch Run Contract, and acknowledges that its
Verification code runs with host authority; the source is still copied into an isolated Run
Workspace and the exact Candidate Patch still requires approval. Path containment limits
agent file access but is not a hostile-code sandbox, so implicit repository discovery and
untrusted repositories remain excluded; ADR-0010 continues to prohibit Trusted Repository
content from Gemini free-tier requests.
