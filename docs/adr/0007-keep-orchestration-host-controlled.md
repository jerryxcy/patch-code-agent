# Keep orchestration host-controlled

LangGraph will own Patch Run phases, transitions, Resource Budgets, and stopping conditions.
Model calls are limited to dedicated nodes that produce a typed Plan, Candidate Patch, or
Diagnosis; a free-running ReAct loop and separate planner, coder, and reviewer agents are
outside the MVP so its control flow remains explicit, observable, and testable without
claiming that external model outputs are deterministic.
