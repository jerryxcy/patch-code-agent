# Isolate each Patch Run in a durable workspace

Each Patch Run will copy its immutable Repository Source into a durable Run Workspace below
a data root that must not overlap the source tree, rather than editing the source or requiring
Git worktrees. The CLI default is `~/.patch-code-agent/runs/<run-id>/workspace`. This preserves
repeatable demonstrations, prevents recursive self-copy when scanning the current repository,
isolates concurrent runs, and gives resumed runs a stable target without adding Git branch
lifecycle management to the MVP.
