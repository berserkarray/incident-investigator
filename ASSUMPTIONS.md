# Assumptions

These assumptions are binding. Keep them true as the implementation evolves.

## Incident data

1. **Post-incident analysis of a fixed data snapshot.** No live streaming or alerting.
2. **The input is a short incident report** (title, symptoms, affected service, start time, severity). Treat its claims as evidence to verify, not as fact.
3. **Each incident simulates 8–15 services. Telemetry (logs, metrics) covers a 2–24 hour window; change history reaches back 7–30 days.**
4. **"Misleading" data means coincidences:** innocent deploys near the incident, noisy alerts, monitors that disagree, look-alike past incidents. There is no adversarial data.
5. **Each incident has one primary root cause,** optionally with contributing factors.

## System behaviour

6. **The system is read-only.** It recommends remediation and never executes it.
7. **Build all six listed agents.** The brief says "at least five" but lists six.
8. **"Genuine collaboration" means three things:**
   - agents share one evidence board;
   - the Critic can send targeted evidence requests back to the specialist agents;
   - conclusions are revised when new evidence arrives.
9. **"Inconclusive" is a valid outcome.** It is scored correct only when the answer key marks the evidence as insufficient.
10. **A fact is something a tool returned. Everything inferred is an assumption,** and the report labels each.

## Tools and evaluation

11. **Tool failures are injected deterministically from a seed:** timeouts, partial results, stale data.
12. **A permanently failed tool becomes a recorded data gap** that lowers confidence. It never aborts the run.
13. **The baseline is one agent in a ReAct loop,** with the same model, tools, budgets and data.
14. **Root-cause accuracy is scored on the answer key's structured fields** (component plus cause type), reported as top-1 and top-3. No LLM-as-judge for accuracy.
15. **Evidence grounding** is the share of cited evidence IDs that exist and support the claim, plus how much of the key evidence was found.
16. **Each incident runs 3 times per system,** reported as mean ± spread.
17. **Latency is end-to-end wall-clock time.** Token counts come from the provider's usage data.

## Delivery

18. **The interface is a CLI** that writes JSON and Markdown reports. No frontend.
19. **Real runs need one LLM API key,** documented as an external dependency. Automated tests use a fake LLM and need no key.
