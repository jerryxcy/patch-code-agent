# Require explicit model selection for external repository transfer

Fixture Repository Patch Runs may use Gemini, but only when the user explicitly selects a supported
Gemini model with `--model`. Model inspection remains bounded, credentials stay local, and the
chosen model must be restored automatically when an interrupted Patch Run resumes. The provider is
initialized only if the resumed graph actually needs more model work.
