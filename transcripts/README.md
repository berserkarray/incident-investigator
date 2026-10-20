# Coding-assistant transcript

This folder holds the transcript of the Kiro (AI coding assistant) session that built this project: spec, design, task list, implementation, integration, the baseline-parity fix and the evaluation run.

The assistant cannot export its own session, so the user exports it from the IDE into this folder (for example `transcripts/kiro-session.md`). Before committing, scan it for API key values, printing file names only:

```bash
set -a; source .env; set +a
grep -rlF -- "$OPENAI_API_KEY" transcripts/ || echo "no key found"
```
