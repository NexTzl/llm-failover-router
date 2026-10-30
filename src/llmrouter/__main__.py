"""Replays a simulated day of traffic through an outage of the primary provider."""
import argparse
from collections import Counter

from .providers import SimulatedProvider
from .router import AllProvidersFailed, BudgetExceeded, Router


class SimClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def build_providers(clock, requests: int, seed: int) -> list[SimulatedProvider]:
    outage = (requests * 0.30, requests * 0.50)
    return [
        SimulatedProvider("primary", "fast-1.0", 0.10, 0.40, failure_rate=0.02,
                          outages=[outage], clock=clock, seed=seed),
        SimulatedProvider("secondary", "open-2.1", 0.30, 0.90, failure_rate=0.05,
                          clock=clock, seed=seed + 1),
        SimulatedProvider("tertiary", "large-3.0", 1.00, 3.00, failure_rate=0.01,
                          clock=clock, seed=seed + 2),
    ]


def replay(providers, clock, requests: int) -> dict:
    router = Router({"support-bot": providers}, budget_usd=1000.0,
                    clock=clock, sleep=clock.sleep)
    served, failed, fallbacks = Counter(), 0, 0
    for i in range(requests):
        clock.now = max(clock.now, float(i))
        try:
            completion = router.complete("support-bot", "Summarize this ticket. " * 20)
        except (AllProvidersFailed, BudgetExceeded):
            failed += 1
            continue
        served[completion.provider] += 1
        fallbacks += completion.fallback
    return {"served": served, "failed": failed, "fallbacks": fallbacks,
            "cost": router.spent_usd,
            "calls": sum(provider.calls for provider in providers)}


def show(label: str, requests: int, result: dict) -> None:
    success = sum(result["served"].values())
    print(f"{label}: {success}/{requests} answered ({success / requests:.2%}), "
          f"{result['failed']} failed, {result['fallbacks']} via fallback, "
          f"{result['calls']} provider calls, ${result['cost']:.4f}")
    print(f"  served by: {dict(sorted(result['served'].items()))}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Simulate an outage")
    parser.add_argument("--requests", type=int, default=5000)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    clock = SimClock()
    single = build_providers(clock, args.requests, args.seed)[:1]
    show("primary only", args.requests, replay(single, clock, args.requests))

    clock = SimClock()
    chain = build_providers(clock, args.requests, args.seed)
    show("with failover", args.requests, replay(chain, clock, args.requests))


if __name__ == "__main__":
    main()
