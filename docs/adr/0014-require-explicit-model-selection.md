# Require explicit model selection for external repository transfer

Fixture and Trusted Repository Patch Runs may use Gemini, but only when the user explicitly
selects a supported Gemini model with `--model`. This consent is separate from
`--trust-repository`, which authorizes local Verification execution rather than external data
transfer; model inspection remains bounded, credentials stay local, and the chosen model must be
restored automatically when an interrupted Patch Run resumes. The provider is initialized only if
the resumed graph actually needs more model work.
