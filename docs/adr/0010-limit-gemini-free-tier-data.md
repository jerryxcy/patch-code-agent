---
status: superseded by ADR-0014
---

# Limit Gemini free-tier data to synthetic fixtures

The opt-in Live Smoke Run will use the Gemini Developer API free tier with
`gemini-3.7-flash`, but only bundled synthetic Fixture Repositories may be sent to it.
Free-tier prompts and responses may be used to improve Google's products or reviewed by
humans, so private code, credentials, personal data, and arbitrary repositories must never
enter model requests; scripted tests remain the required verification path.
