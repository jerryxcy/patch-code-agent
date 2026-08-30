# Protect the Verification boundary

The model may read acceptance tests but may modify only the exact paths declared editable
by the Fixture Manifest; tests, the Issue, and the manifest remain immutable. Verification
runs trusted synthetic code with argv rather than a shell and receives a minimal
environment that excludes Gemini credentials and other secrets, preventing either side of
the model–verifier boundary from rewriting the acceptance contract or inheriting harness
authority.
