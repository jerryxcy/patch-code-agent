# Protect the Verification boundary

The model may read acceptance tests but may modify only the exact paths declared editable
by the Patch Run Contract; tests, the Issue, and the contract remain immutable. Verification
runs explicitly trusted Repository Source code with argv rather than a shell and receives a
minimal environment that excludes model credentials and other secrets, preventing either
side of the model–verifier boundary from rewriting the acceptance contract or inheriting
harness authority.
