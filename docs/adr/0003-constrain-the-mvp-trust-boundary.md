# Constrain the MVP trust boundary

The prototype operates only on registered, bundled Fixture Repositories. Each fixture provides a
validated `patch-run.toml`; its contract is loaded before execution and remains protected in the
isolated Run Workspace. The exact Candidate Patch still requires approval. Path containment limits
agent file access but is not a hostile-code sandbox, so local repository selection, implicit
repository discovery, and untrusted repositories remain excluded. Sending Fixture Repository
content to Gemini requires the explicit model selection recorded in ADR-0014.
