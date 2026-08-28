# PatchCodeAgent

> A test-driven coding agent harness built with LangGraph.

PatchCodeAgent is a one-day side project for learning the engineering around coding
agents: explicit state, constrained tools, approval gates, checkpointing,
verification feedback, bounded retries, and measurable run reports.

The initial scaffold contains a small, read-only LangGraph workflow. It validates
the task, inventories Python files, creates a starter plan, and emits structured
state. The next milestone is to replace the starter nodes with model-backed
planning, patching, and test-verification nodes.

## Quick start

```bash
uv sync --dev
uv run patch-code-agent run "Fix the failing cart discount tests" --repo examples/tiny_repo
uv run pytest
```

Expected flow:

```text
issue + repository
        |
        v
 validate_input -> inspect_repo -> create_plan -> approval
                                             |
                                             v
                                      apply_patch
                                             |
                                             v
                                        run_tests
                                        /       \
                                     pass       fail
                                      |           |
                                    report     diagnose
                                                  |
                                           bounded retry
```

## One-day scope

- [x] Python package and CLI
- [x] Typed graph state
- [x] Checkpoint-enabled graph scaffold
- [x] Smoke test and tiny fixture repository
- [ ] Model-backed structured plan
- [ ] Workspace-scoped read and patch tools
- [ ] Human approval before writes
- [ ] `pytest` verification feedback loop
- [ ] Maximum of three repair attempts
- [ ] JSONL trajectory and final run report
- [ ] Naive-agent versus harness comparison

## Harness rules

The MVP should remain deliberately constrained:

- Only access the selected repository directory.
- Prefer narrow tools over arbitrary shell access.
- Require approval before changing files.
- Treat test execution as the source of truth.
- Limit attempts, files changed, tool calls, and wall-clock time.
- Record enough state to explain and reproduce every run.

## Proposed run report

```json
{
  "success": true,
  "attempts": 2,
  "files_changed": ["src/cart.py"],
  "tests_before": {"passed": 9, "failed": 3},
  "tests_after": {"passed": 12, "failed": 0},
  "tool_calls": 11,
  "duration_seconds": 43.2
}
```
