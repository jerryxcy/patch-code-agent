# PatchCodeAgent

English | [繁體中文](./README.zh-TW.md)

This is a test-driven coding agent harness for learning LangGraph. It uses one bundled exercise to
demonstrate conditional routing, interrupt/resume, persistent state, and human-in-the-loop
approval.

Instead of giving a model unrestricted shell access, PatchCodeAgent breaks a code repair into an
observable, pausable, reviewable, and verifiable **Patch Run**. PatchCodeAgent controls the flow,
modifies files, and runs tests; the model only proposes a Plan, Candidate Patch, and Diagnosis.

---

## Architecture

```mermaid
flowchart TD
    human["User"] --> cli["PatchCodeAgent CLI"]

    cli -->|"command: patch-code-agent run cart-discount<br/>optionally add --model gemini-..."| fixture["Bundled exercise repository<br/>Fixture Repository<br/>Example: cart-discount"]

    fixture -->|"Read the issue, test command,<br/>and editable files"| app["Prepare and run one Patch Run<br/>PatchCodeAgent"]
    app -->|"Copy without modifying the source"| workspace["Isolated copy for this run<br/>Run Workspace"]
    app --> workflow["Repair workflow<br/>LangGraph"]
    workspace <--> workflow

    model["Selected model<br/>Automated tests: Scripted Model<br/>External model: Gemini"] -.->|"Only proposes a Plan,<br/>Patch, and Diagnosis"| workflow
    workflow --> candidate["Proposed change awaiting review<br/>Candidate Patch"]
    candidate --> approval{"Apply this change?<br/>Approval Gate"}
    cli -->|"command: patch-code-agent approve RUN_ID"| approval
    cli -->|"command: patch-code-agent reject RUN_ID"| approval

    approval -->|"approve"| apply["Apply changes to the<br/>Run Workspace"]
    apply --> workspace
    apply --> verification["Run the configured test command<br/>Example: pytest"]
    verification -->|"fail: diagnose and propose another change"| workflow
    verification -->|"pass"| succeeded["Repair succeeded<br/>End Patch Run"]
    approval -->|"reject"| rejected["Leave the Workspace unchanged<br/>End Patch Run"]

    approval -.->|"Pause and persist"| storage[("Run records<br/>state · diff · logs · report")]
    succeeded --> storage
    rejected --> storage
    cli -->|"command: patch-code-agent status RUN_ID"| status["Read the current state<br/>PatchRunStatusReader"]
    status --> storage
    workflow <--> storage
```

| Module | Responsibility |
|---|---|
| **Fixture Repository** | A bundled exercise such as `cart-discount`; useful for a first run and automated tests |
| **Run Workspace** | An isolated copy of the source; all changes happen here, never in the source fixture |
| **LangGraph** | Runs verification, planning, patch generation, approval, patch application, and verification again |
| **Scripted Model / Gemini** | Uses the offline Scripted Model unless `--model` selects Gemini to propose a Plan, Patch, and Diagnosis |
| **Run records** | Persist state, diffs, test logs, and the final report for `status` and later resume operations |

See [docs/design.md](./docs/design.md) for the full state machine, tool and trust boundaries,
Approval and replay safety, fixed safety limits, artifact layout, and Run Report schema.

### Patch Run graph

The Mermaid graph below directly reflects the nodes, edges, and conditional routes compiled by
`build_graph()`:

```mermaid
---
config:
  flowchart:
    curve: linear
---
graph TD;
    __start__([<p>__start__</p>]):::first
    validate_input(validate_input)
    baseline_verification(baseline_verification)
    create_plan(create_plan)
    create_candidate(create_candidate)
    approval_gate(approval_gate)
    reject_candidate(reject_candidate)
    apply_candidate(apply_candidate)
    repair_verification(repair_verification)
    create_diagnosis(create_diagnosis)
    finalize_report(finalize_report)
    __end__([<p>__end__</p>]):::last
    __start__ --> validate_input;
    apply_candidate -. apply_failed .-> finalize_report;
    apply_candidate -. verify .-> repair_verification;
    approval_gate -. approve .-> apply_candidate;
    approval_gate -. reject .-> reject_candidate;
    baseline_verification -.-> create_plan;
    baseline_verification -. finish_without_repair .-> finalize_report;
    create_candidate -. wait_for_approval .-> approval_gate;
    create_candidate -. candidate_failed .-> finalize_report;
    create_diagnosis -. retry .-> create_candidate;
    create_diagnosis -. cannot_retry .-> finalize_report;
    create_plan -. candidate .-> create_candidate;
    create_plan -. plan_failed .-> finalize_report;
    reject_candidate --> finalize_report;
    repair_verification -. diagnose .-> create_diagnosis;
    repair_verification -. finish_verification .-> finalize_report;
    validate_input --> baseline_verification;
    finalize_report --> __end__;
    classDef default fill:#f2f0ff,line-height:1.2
    classDef first fill-opacity:0
    classDef last fill:#bfb6fc
```

After changing the graph, run this command to print the latest Mermaid Markdown for updating the
diagram above:

```bash
uv run python scripts/render_graph.py
```

---

## Quick start

PatchCodeAgent requires **Python 3.12+** and [uv](https://docs.astral.sh/uv/).

### Try the bundled exercise

```bash
# Install runtime and development dependencies.
uv sync --dev

# List the bundled exercises that are ready to run.
uv run patch-code-agent fixtures

# Start a Patch Run for cart-discount and note the printed Run Identifier.
uv run patch-code-agent run cart-discount

# Paste the Run Identifier from the previous command here.
RUN_ID="paste the Run Identifier here"

# Inspect the Run state, Plan, and Candidate Patch awaiting approval.
uv run patch-code-agent status "$RUN_ID"

# Approve the Candidate Patch, apply it, and rerun the tests.
uv run patch-code-agent approve "$RUN_ID" --yes

# Inspect the final state and test result.
uv run patch-code-agent status "$RUN_ID"

# To try rejection, start a separate Patch Run that will not affect the previous result.
uv run patch-code-agent run cart-discount

# Paste the new Run Identifier here.
NEW_RUN_ID="paste the new Run Identifier here"

# Reject the Candidate Patch.
uv run patch-code-agent reject "$NEW_RUN_ID"

# Confirm that the Run ended without applying the Candidate Patch to its workspace.
uv run patch-code-agent status "$NEW_RUN_ID"
```

When the approval flow finishes, `Outcome: Succeeded` and `Verification: passed` mean the repair
succeeded. The CLI prints full paths to the modified files, Verification log, `cumulative.diff`,
and `report.json` so you can inspect the result.

### Use Gemini

To let Gemini read the code and propose a Candidate Patch, install the optional dependency and
provide a Google AI Studio key through an environment variable:

```bash
# Install the Gemini integration.
uv sync --extra gemini

# Create a local environment file, then add your GEMINI_API_KEY to .env.
cp .env.example .env

# Use Gemini for the bundled exercise.
uv run patch-code-agent run cart-discount --model gemini-3.7-flash
```

The graph still pauses at the Approval Gate after Gemini produces a Candidate Patch. Note the Run
Identifier, then continue with the `status` and `approve` commands from the previous section.

Available CLI commands:

```text
patch-code-agent fixtures
patch-code-agent run cart-discount [--model gemini-3.7-flash]
patch-code-agent status <run-id>
patch-code-agent approve <run-id> [--yes]
patch-code-agent reject <run-id>
```

---

## Development and testing

| Command | Purpose |
|---|---|
| `uv sync --dev` | Install runtime and development dependencies |
| `uv run pytest` | Run the graph and CLI acceptance tests |
| `uv run ruff check .` | Run the Python linter |
| `uv run python scripts/render_graph.py` | Print Mermaid Markdown from the compiled graph |
| `uv run patch-code-agent run cart-discount` | Create an isolated Patch Run |
| `uv run pytest examples/tiny_repo/test_cart.py` | Run the fixture baseline; it is expected to fail |

---

## Project structure

```text
src/patch_code_agent/
  __main__.py          Entry point for python -m patch_code_agent
  application.py       Application seam for fixtures, workspaces, and checkpoints
  candidate.py         Structured replacement validation, exact diffs, and replay ledger
  cli.py               Typer CLI and Rich output
  diagnosis.py         Typed Diagnosis, failure evidence, and replay ledger
  fixtures/            Discovery and registry for bundled Fixture Repositories
  gemini.py             Gemini transport, tool loop, and retries
  graph.py              LangGraph nodes, edges, and checkpoint assembly
  inspection.py         Bounded list, read, and search tools plus workspace safety rules
  limits.py             Fixed safety limits for repairs, files, tools, and model requests
  model.py              Model inputs, outputs, and the offline Scripted Model
  model_output.py       Structured output validation and correction retry
  patching.py           Replay-safe replacement apply, preimage classification, and cumulative diff
  planning.py           Typed Plan validation, artifact checksum, and replay ledger
  reporting.py          Run Events and the final report.json
  sources.py            Fixture Patch Run Manifest, Repository Source, and validation
  state.py              Patch Run graph state
  verification.py      Baseline/Repair Verification, outcome classification, and replay-safe logs
  workspace.py         Rules for creating isolated Run Workspaces

tests/
  test_cli.py          Registry, workspace, baseline outcomes, artifacts, and durable status
  test_gemini.py       Gemini transport contract, tool circulation, retries, and request limit
  test_graph.py        Graph smoke test

scripts/
  render_graph.py      Generate Mermaid Markdown from the compiled graph

examples/tiny_repo/
  patch-run.toml       Issue, Verification command, and editable paths for the bundled exercise
  issue.md             Cart discount Issue
  cart.py              Deliberately incorrect implementation
  test_cart.py         Fixture baseline and acceptance test

CONTEXT.md             PatchCodeAgent domain glossary
docs/design.md         State machine, boundaries, safety limits, artifacts, and report schema
docs/adr/              Individual architectural decisions and tradeoffs
docs/agents/           Repository configuration for engineering skills
AGENTS.md              Entry point for tracker and domain documentation used by agents
pyproject.toml          Package, dependency, pytest, and Ruff configuration
uv.lock                Locked dependencies
```

---

## Further reading

For the complete graph lifecycle, tool limits, Approval flow, replay safety, artifacts, and Run
Report design, see [docs/design.md](./docs/design.md).
