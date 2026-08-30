# Make Patch Runs resumable across processes

A Patch Run must survive CLI process exit at an Approval Gate and resume later from its
Run Identifier. PatchCodeAgent passes that same value to LangGraph internally as its
`thread_id`. This adds persistence and resume semantics to the MVP, but makes the
checkpoint and explicit-state claims observable instead of limiting them to an in-memory
implementation detail.
