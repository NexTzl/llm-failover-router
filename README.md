# llm-failover-router

A single access layer for LLM providers. Each service pins its own ordered list of models, and the router handles retries, failover, circuit breaking, budget limits and audit logs in one place.

It has no runtime dependencies. The included HTTP adapter works with any endpoint that speaks the OpenAI chat completions format. The tests and the simulation use simulated providers, so they run without API keys or cost.

```text
                     +----------- for each pinned provider, in order -----------+
                     |                                                          |
service --> budget --+--> circuit open? --> call --> success --> log cost       |
            check    |         | yes         |                                  |
                     |         v             +--> transient --> retry w/ backoff|
                     |       skip            +--> permanent --> next provider   |
                     +----------------------------------------------------------+
                                     all failed --> explicit error
```

## Run it

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
python -m llmrouter
```

## Simulated outage

The simulation sends 5,000 requests while the primary provider is down for 20% of the period. Failure rates and prices are invented for the example.

| Setup | Answered | Failed | Via fallback | Provider calls | Simulated cost |
|---|---|---|---|---|---|
| Primary only | 3,978 (79.56%) | 1,022 | 0 | 4,076 | $0.3013 |
| With failover | 5,000 (100%) | 0 | 1,023 | 5,162 | $0.4856 |

The circuit breaker is why the outage costs so few extra calls. After repeated failures the router stops calling the primary, then sends a single probe after each cooldown.

## Usage

```python
from llmrouter import HttpProvider, Router
from llmrouter.router import jsonl_sink

primary = HttpProvider("primary", "model-a-2026-01", "https://api.example.com/v1",
                       api_key="...", input_price=0.10, output_price=0.40)
backup = HttpProvider("backup", "model-b-2026-03", "https://api.example.org/v1",
                      api_key="...", input_price=0.30, output_price=0.90)

router = Router(
    routes={"support-bot": [primary, backup], "tagging": [primary]},
    budget_usd=20.0,
    sink=jsonl_sink("llm-audit.jsonl"),
)

answer = router.complete("support-bot", "Summarize this ticket: ...")
print(answer.text, answer.provider, answer.cost_usd)
```

## Design decisions

| Decision | Why |
|---|---|
| Models pinned per service | An upgrade or rollback touches one service, and a provider's default change cannot alter behavior silently. |
| Transient and permanent errors treated differently | A 429 is worth retrying. A bad key or a retired model is not, so the router moves on at once. |
| Circuit breaker per provider | A provider that is down stops adding latency to every request, and traffic returns after a successful probe. |
| Budget checked before each call | A loop or traffic spike hits a hard daily limit instead of an unexpected bill. |
| Audit log with a prompt fingerprint only | Prompts may contain personal data. The log can correlate requests without storing the text. |
| Explicit error when every provider fails | Callers decide what to do, and an empty answer never passes as a real one. |

## Project layout

```text
src/llmrouter/breaker.py     circuit breaker
src/llmrouter/providers.py   provider contract, HTTP adapter and simulator
src/llmrouter/router.py      routing, retries, budget and audit log
src/llmrouter/__main__.py    outage simulation
tests/test_router.py         behavior and failure scenarios
```

## Limitations

- Breaker and budget state live in memory, so they reset on restart and are not shared between processes. A multi-instance setup would need a shared store such as Redis.
- It is not thread-safe.
- Streaming is not supported.
- The budget is checked before a call, so the last request of the day can pass the limit slightly.

## License

MIT
