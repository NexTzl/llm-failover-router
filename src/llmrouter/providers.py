"""Provider contract, a simulated provider and an OpenAI-compatible HTTP adapter."""
import json
import random
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Protocol


class TransientError(Exception):
    """Timeout, 429 or 5xx. Worth retrying."""


class PermanentError(Exception):
    """Bad key, retired model or invalid request. Retrying cannot help."""


@dataclass(frozen=True)
class Reply:
    text: str
    input_tokens: int
    output_tokens: int


class Provider(Protocol):
    name: str
    model: str
    input_price: float   # USD per million tokens
    output_price: float

    def complete(self, prompt: str) -> Reply: ...


def classify_status(status: int) -> type[Exception]:
    return TransientError if status == 429 or status >= 500 else PermanentError


@dataclass
class HttpProvider:
    """Any endpoint that speaks the OpenAI chat completions format."""
    name: str
    model: str
    base_url: str
    api_key: str
    input_price: float
    output_price: float
    timeout: float = 30.0

    def complete(self, prompt: str) -> Reply:
        request = urllib.request.Request(
            f"{self.base_url.rstrip('/')}/chat/completions",
            data=json.dumps({
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
            }).encode(),
            headers={"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.load(response)
        except urllib.error.HTTPError as error:
            raise classify_status(error.code)(f"{self.name}: HTTP {error.code}") from error
        except (urllib.error.URLError, TimeoutError) as error:
            raise TransientError(f"{self.name}: {error}") from error
        usage = body.get("usage", {})
        return Reply(body["choices"][0]["message"]["content"],
                     usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))


@dataclass
class SimulatedProvider:
    """Deterministic stand-in with configurable failures and outage windows."""
    name: str
    model: str
    input_price: float
    output_price: float
    failure_rate: float = 0.0
    outages: list[tuple[float, float]] = field(default_factory=list)
    clock: Callable[[], float] = lambda: 0.0
    seed: int = 0
    calls: int = 0

    def __post_init__(self):
        self._rng = random.Random(self.seed)

    def complete(self, prompt: str) -> Reply:
        self.calls += 1
        now = self.clock()
        if any(start <= now < end for start, end in self.outages):
            raise TransientError(f"{self.name}: 503 during outage")
        if self._rng.random() < self.failure_rate:
            raise TransientError(f"{self.name}: 429 rate limited")
        return Reply(f"answer from {self.name}",
                     max(1, len(prompt) // 4), self._rng.randrange(80, 240))
