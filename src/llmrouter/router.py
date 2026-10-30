"""Routes each request through the service's pinned providers, in order."""
import hashlib
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from .breaker import CircuitBreaker
from .providers import PermanentError, Provider, TransientError


class AllProvidersFailed(Exception):
    pass


class BudgetExceeded(Exception):
    pass


@dataclass(frozen=True)
class Completion:
    text: str
    provider: str
    model: str
    cost_usd: float
    attempts: int
    fallback: bool


class Router:
    def __init__(self, routes: dict[str, list[Provider]], budget_usd: float,
                 max_attempts: int = 3, breaker_threshold: int = 5,
                 breaker_cooldown: float = 60.0, backoff: float = 0.5,
                 clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep,
                 sink: Callable[[dict], None] = lambda event: None):
        self.routes = routes
        self.budget_usd = budget_usd
        self.spent_usd = 0.0
        self.max_attempts = max_attempts
        self.backoff = backoff
        self.clock = clock
        self.sleep = sleep
        self.sink = sink
        self._day = self._today()
        self.breakers = {
            provider.name: CircuitBreaker(breaker_threshold, breaker_cooldown, clock)
            for providers in routes.values() for provider in providers
        }

    def _today(self) -> str:
        moment = datetime.fromtimestamp(self.clock(), timezone.utc)
        return moment.strftime("%Y-%m-%d")

    def _check_budget(self) -> None:
        if self._today() != self._day:
            self._day, self.spent_usd = self._today(), 0.0
        if self.spent_usd >= self.budget_usd:
            raise BudgetExceeded(f"daily limit of ${self.budget_usd:.2f} reached")

    def _log(self, service: str, prompt: str, event: str, **fields) -> None:
        # Prompts may hold personal data, so the log keeps only a fingerprint.
        fingerprint = hashlib.sha256(prompt.encode()).hexdigest()[:12]
        self.sink({"at": self.clock(), "service": service, "prompt_sha": fingerprint,
                   "event": event, **fields})

    def complete(self, service: str, prompt: str) -> Completion:
        self._check_budget()
        attempts = 0
        for position, provider in enumerate(self.routes[service]):
            breaker = self.breakers[provider.name]
            if not breaker.allow():
                self._log(service, prompt, "skipped_open_circuit", provider=provider.name)
                continue
            for attempt in range(1, self.max_attempts + 1):
                attempts += 1
                try:
                    reply = provider.complete(prompt)
                except TransientError as error:
                    breaker.record_failure()
                    self._log(service, prompt, "transient_failure",
                              provider=provider.name, attempt=attempt, error=str(error))
                    if not breaker.allow():
                        break
                    if attempt < self.max_attempts:
                        self.sleep(self.backoff * 2 ** (attempt - 1))
                except PermanentError as error:
                    breaker.record_failure()
                    self._log(service, prompt, "permanent_failure",
                              provider=provider.name, error=str(error))
                    break
                else:
                    breaker.record_success()
                    cost = (reply.input_tokens * provider.input_price
                            + reply.output_tokens * provider.output_price) / 1_000_000
                    self.spent_usd += cost
                    self._log(service, prompt, "success", provider=provider.name,
                              model=provider.model, cost_usd=round(cost, 8),
                              attempts=attempts, fallback=position > 0)
                    return Completion(reply.text, provider.name, provider.model,
                                      cost, attempts, position > 0)
        self._log(service, prompt, "exhausted", attempts=attempts)
        raise AllProvidersFailed(f"no provider answered for service '{service}'")


def jsonl_sink(path: str) -> Callable[[dict], None]:
    def write(event: dict) -> None:
        with open(path, "a", encoding="utf-8") as log:
            log.write(json.dumps(event) + "\n")
    return write
