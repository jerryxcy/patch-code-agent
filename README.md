# PatchCodeAgent

> A test-driven coding agent harness built with LangGraph.

PatchCodeAgent is a one-day side project for learning the engineering around coding
agents: explicit state, constrained tools, approval gates, checkpointing,
verification feedback, bounded retries, and measurable run reports.

The current scaffold contains a small, read-only LangGraph workflow. It validates
the task, inventories Python files, creates a starter plan, and emits structured
state. The agreed MVP below is a design target and is not implemented yet.

## Current scaffold quick start

```bash
uv sync --dev
uv run patch-code-agent run "Fix the failing cart discount tests" --repo examples/tiny_repo
uv run pytest
```

Target MVP flow:

```text
Fixture Repository
        |
        v
initialize -> create Run Workspace -> baseline Verification
                                          |-- pass  -> Invalid Fixture
                                          |-- error -> Error
                                          `-- fail
                                               |
                                               v
                         inspect -> create Plan -> propose and persist Candidate Patch
                                          |
                                          v
                                    Approval Gate
                                    /             \
                              reject               approve
                                |                     |
                                v                     v
                            Rejected       validate and apply
                                                      |
                                                      v
                                                Verification
                                                /            \
                                           pass                fail
                                            |                    |
                                            v                    v
                                       Succeeded       budget or attempts spent?
                                                           /            \
                                                         yes             no
                                                          |               |
                                                          v               v
                                               terminal outcome       Diagnosis
                                                                          |
                                                                          +-> Candidate Patch

Every terminal outcome -> finalize Run Report
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

## Agreed MVP contract

- Operate only on registered, immutable, synthetic fixture repositories.
- Copy each fixture into a durable `runs/<run-id>/workspace` and never write back to the
  source fixture.
- Run a failing pytest baseline before making any model request.
- Use Gemini 3.7 Flash through the Gemini Developer API only for an opt-in live smoke run;
  required automated tests use a scripted model.
- Give the model bounded list, read, and search tools, but no shell or direct write access.
- Accept only structured replacements of existing, previously read, explicitly editable
  text files.
- Reject symlinks at every path segment and read only regular UTF-8 text files whose
  resolved paths remain inside the Run Workspace. Exclude `.git`, virtual environments,
  caches, build output, and hidden directories.
- Limit each readable file to 100 KiB and each search response to 32 KiB. Store complete
  Verification stdout and stderr as a Run Artifact, but expose at most a 32 KiB failure
  excerpt to the model and Checkpoint.
- Persist an exact candidate diff and checksum, then stop at a cross-process approval gate
  before applying it.
- Retain failed approved changes and use verification output to diagnose the next candidate,
  for at most three repair attempts.
- Store resumable control state in SQLite and keep workspaces, events, diffs, verification
  logs, and versioned reports as filesystem artifacts.
- Produce an explicit terminal outcome and report for success, rejection, invalid fixtures,
  exhausted attempts, exceeded budgets, workspace changes, and errors.
- Preserve successful repairs in the run workspace and report their cumulative diff; do not
  create commits or branches.

### Resource Budgets

- At most 3 Repair Attempts
- At most 12 distinct files read
- At most 3 files changed
- At most 20 tool executions
- At most 8 model requests, including provider retries
- At most 60 seconds per Verification
- At most 5 minutes of active Patch Run time, excluding time paused at an Approval Gate

An invalid typed model response receives one schema-correction request before the Patch Run
ends as `Error` with `error_kind: invalid_model_output`. Gemini requests may retry transient
provider failures twice within the model-request and active-time budgets. Free-tier quota
failure makes only the opt-in Live Smoke Run inconclusive.

For pytest Verification, exit code `0` passes, `1` is a repairable test failure, and exit
codes `2` through `5` are Verification errors. A passing baseline produces Invalid Fixture;
a 60-second timeout produces Budget Exceeded.

Target CLI:

```text
patch-code-agent fixtures
patch-code-agent run cart-discount
patch-code-agent status <run-id>
patch-code-agent approve <run-id>
patch-code-agent reject <run-id>
```

`approve` re-displays the immutable Candidate Patch diff and checksum, then prompts with No
as the default. Automation must pass `--yes` explicitly. Interactive and automated
approval both revalidate the candidate checksum, file preimage hashes, and Run Workspace
state before resuming; `--yes` never bypasses those preconditions.

The MVP is complete when Scripted Model integration tests cover the success, diagnosis,
resume,
rejection, invalid-fixture, attempts-exhausted, budget-exceeded, and workspace-changed
paths; the opt-in Gemini smoke run can repair the cart fixture; pytest, Ruff, and CLI smoke
checks pass; and the documentation matches actual behavior. Provider quota failures make
the optional Live Smoke Run inconclusive rather than failing the required test suite.

## Non-goals for the MVP

- Naive-agent versus harness comparison
- Arbitrary or untrusted repositories
- A hostile-code execution sandbox
- Git branch or commit integration
- Provider-agnostic model support
- Automatic run retention or cleanup

## Harness rules

The MVP should remain deliberately constrained:

- Only access a registered Fixture Repository through its isolated Run Workspace.
- Prefer narrow tools over arbitrary shell access.
- Stop at an Approval Gate before changing files.
- Treat Verification as the source of truth.
- Enforce every Resource Budget in the host-controlled graph.
- Record enough Checkpoints, Run Events, and Run Artifacts to explain and resume every Patch
  Run.

## Target Run Report

```json
{
  "schema_version": "1",
  "run_id": "<run-id>",
  "fixture_id": "cart-discount",
  "model_id": "gemini-3.7-flash",
  "outcome": "succeeded",
  "terminal_reason": null,
  "error_kind": null,
  "started_at": "2026-08-30T06:00:00Z",
  "finished_at": "2026-08-30T06:00:43Z",
  "active_duration_seconds": 43.2,
  "attempts": 2,
  "model_requests": 6,
  "tool_executions": 11,
  "files_read": ["cart.py", "test_cart.py"],
  "files_changed": ["cart.py"],
  "verification": {
    "baseline": {
      "outcome": "failed",
      "exit_code": 1,
      "duration_seconds": 0.8,
      "artifact": "baseline/output.log"
    },
    "attempts": [
      {
        "attempt": 1,
        "outcome": "failed",
        "exit_code": 1,
        "duration_seconds": 0.9,
        "artifact": "attempts/1/verification.log"
      },
      {
        "attempt": 2,
        "outcome": "passed",
        "exit_code": 0,
        "duration_seconds": 0.7,
        "artifact": "attempts/2/verification.log"
      }
    ]
  },
  "artifacts": {
    "plan": {"path": "plan.json", "sha256": "<sha256>"},
    "diagnoses": [{"path": "attempts/1/diagnosis.json", "sha256": "<sha256>"}],
    "candidates": [
      {"path": "attempts/1/candidate.json", "sha256": "<sha256>"},
      {"path": "attempts/2/candidate.json", "sha256": "<sha256>"}
    ],
    "cumulative_diff": {"path": "cumulative.diff", "sha256": "<sha256>"}
  },
  "budgets": {
    "repair_attempts": {"limit": 3, "used": 2},
    "model_requests": {"limit": 8, "used": 6},
    "tool_executions": {"limit": 20, "used": 11},
    "files_read": {"limit": 12, "used": 2},
    "files_changed": {"limit": 3, "used": 1},
    "verification_seconds": {"limit": 60, "used_max": 0.9},
    "active_seconds": {"limit": 300, "used": 43.2}
  }
}
```
