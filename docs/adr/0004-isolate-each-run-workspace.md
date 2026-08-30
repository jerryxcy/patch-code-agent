# Isolate each Patch Run in a durable workspace

Each Patch Run will copy its immutable Fixture Repository into a durable
`runs/<run-id>/workspace` rather than editing the fixture or requiring Git worktrees.
This preserves repeatable demonstrations, isolates concurrent runs, and gives resumed runs
a stable target without adding Git branch lifecycle management to the MVP.
