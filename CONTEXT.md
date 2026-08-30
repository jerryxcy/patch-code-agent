# PatchCodeAgent

PatchCodeAgent is a constrained environment for running and evaluating automated
repository repairs. This glossary names the concepts used to discuss its behavior.

## Language

**Patch Run**:
A resumable effort to resolve one Issue from a Fixture Repository in an isolated Run
Workspace, including planning, approval, Repair Attempts, and its final outcome.
_Avoid_: Session, job, agent run

**Run Identifier**:
The public, stable identifier used to inspect and resume one Patch Run.
_Avoid_: Thread ID, checkpoint ID

**Issue**:
The bug report or coding task that defines the intended outcome of a Patch Run.
_Avoid_: Prompt, request

**Plan**:
The model's structured explanation of the Issue, relevant files, intended repair, and
Verification approach before it proposes a Candidate Patch.
_Avoid_: Rationale, todo list

**Diagnosis**:
The model's structured explanation of why the preceding Repair Attempt failed Verification
and what the next Candidate Patch must address.
_Avoid_: Replan, retry prompt, error analysis

**Repair Attempt**:
An approved Candidate Patch applied to the Run Workspace and followed by Verification.
A rejected Candidate Patch is not an attempt.
_Avoid_: Retry, iteration

**Candidate Patch**:
An exact proposed diff that replaces existing, previously read editable files and cannot
change the Run Workspace until accepted at an Approval Gate.
_Avoid_: Edit, fix, model output

**Approval Gate**:
The point at which a human accepts or rejects an immutable Candidate Patch, identified by
its exact diff and checksum, before it is applied to the Run Workspace.
_Avoid_: Confirmation, permission prompt

**Verification**:
Execution of the Fixture Repository's declared acceptance command against a Run Workspace.
_Avoid_: Test run, check, evaluation

**Run Report**:
The versioned, structured outcome, measurements, budgets, and artifact references produced
by every completed Patch Run.
_Avoid_: Result, log, summary

**Run Event**:
An append-only, uniquely identified record of a meaningful Patch Run transition or
interaction that remains singular when graph work is replayed.
_Avoid_: Log line, checkpoint

**Run Artifact**:
An immutable file produced by a Patch Run, such as a Candidate Patch, Verification output,
or Run Report.
_Avoid_: Checkpoint, output file

**Checkpoint**:
The persisted control state required to resume a Patch Run at the correct graph position.
It is not the Patch Run's audit history or artifact store.
_Avoid_: Snapshot, log, report

**Resource Budget**:
The fixed limits on model requests, tool executions, file access, file changes,
Verification duration, Repair Attempts, and active Patch Run duration; time paused at an
Approval Gate is excluded.
_Avoid_: Rate limit, quota, retry limit

**Fixture Repository**:
A small, immutable, registered synthetic repository with a known Issue and declared
Verification command whose baseline is expected to fail, used as the input to Patch Runs.
_Avoid_: Example repo, sample project, test repo

**Fixture Manifest**:
The validated contract that identifies a Fixture Repository's Issue, argv-based
Verification command, and exact set of editable paths.
_Avoid_: Config file, metadata

**Run Workspace**:
The isolated, durable copy of a Fixture Repository that one Patch Run may inspect, modify,
verify, and resume.
_Avoid_: Selected repository, working directory, repo path

**Naive Baseline**:
The minimally constrained coding-agent behavior used as the comparison point for
PatchCodeAgent's harness.
_Avoid_: Control agent, ReAct agent

**Scripted Model**:
A deterministic model substitute that returns predefined tool calls and typed artifacts
to exercise Patch Run behavior without an external API.
_Avoid_: Mock model, fake response

**Live Smoke Run**:
An opt-in integration check that uses the configured external model against a synthetic
Fixture Repository; provider unavailability makes the check inconclusive.
_Avoid_: Live test, benchmark, production run

## Outcomes

**Succeeded**:
A Patch Run whose baseline Verification failed and whose final Verification passed.
_Avoid_: Passed, fixed

**Rejected**:
A Patch Run ended because a human rejected its Candidate Patch at an Approval Gate.
_Avoid_: Denied, cancelled

**Invalid Fixture**:
A Patch Run that cannot demonstrate a repair because its baseline Verification passed.
_Avoid_: Already passing, no-op

**Attempts Exhausted**:
A Patch Run that used every permitted Repair Attempt without successful Verification.
_Avoid_: Max retries, failed

**Budget Exceeded**:
A Patch Run stopped after crossing a resource limit other than its Repair Attempt limit.
_Avoid_: Timeout, rate limited

**Workspace Changed**:
A Patch Run stopped because the Run Workspace no longer matched the Candidate Patch that
was presented at its Approval Gate.
_Avoid_: Patch conflict, stale diff

**Error**:
A Patch Run stopped by an unexpected model, storage, patching, or Verification failure
rather than a repair outcome.
_Avoid_: Failed
