# Use LangGraph to expose the harness

The MVP will build its repair orchestration directly with LangGraph rather than begin with
Deep Agents. Deep Agents already packages planning, filesystem, context management, and
subagent behavior that this project exists to make explicit; using the lower-level graph
keeps state, tool boundaries, approval, retry, checkpointing, and evaluation visible for
learning and interview discussion.
