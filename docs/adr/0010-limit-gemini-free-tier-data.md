---
status: superseded by ADR-0014
---

# Limit Gemini free-tier data to synthetic fixtures

Gemini Patch Runs may use the Gemini Developer API free tier, but only bundled synthetic
Fixture Repositories may be sent to it.
Free-tier prompts and responses may be used to improve Google's products or reviewed by
humans, so private code, credentials, personal data, and arbitrary repositories must never
enter model requests; scripted tests remain the required verification path.
