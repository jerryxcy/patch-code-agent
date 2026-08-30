# Make Patch Run mutations replay-safe

Mutating CLI commands will take an exclusive per-run lock, and patch application will be
idempotent under LangGraph replay by validating every replacement's before and after hash
before writing. All-before permits application, all-after means application already
completed, external mismatches stop as Workspace Changed, and a mixed partial application
stops as an explicit error rather than guessing how to continue. Append-only Run Events
use stable identifiers and are written only when absent so replay cannot duplicate audit
records or inflate counters.
