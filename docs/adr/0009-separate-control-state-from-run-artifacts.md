# Separate control state from Run Artifacts

PatchCodeAgent will store resumable LangGraph control state in a harness-owned SQLite
checkpointer while keeping Run Workspaces and immutable Run Artifacts on the filesystem.
Checkpoints contain bounded JSON-serializable values and artifact identifiers or hashes;
append-only events, complete diffs, model transcripts, Verification output, and the final
Run Report remain human-inspectable files below the external data root (by default
`~/.patch-code-agent/runs/<run-id>/`), which must not overlap a Repository Source.
