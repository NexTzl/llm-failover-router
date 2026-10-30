import io
import json
import urllib.error

import pytest

from llmrouter import (AllProvidersFailed, BudgetExceeded, CircuitBreaker, HttpProvider,
                       PermanentError, Router, TransientError)
from llmrouter.providers import Reply, classify_status


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class Scripted:
    """Runs through a list of exceptions, then keeps answering."""

    def __init__(self, name, script=(), input_price=1.0, output_price=2.0):
        self.name, self.model = name, f"{name}-v1"
        self.input_price, self.output_price = input_price, output_price
        self.script, self.calls = list(script), 0

    def complete(self, prompt):
        self.calls += 1
        if self.script:
            error = self.script.pop(0)
            if error:
                raise error
        return Reply("ok", 1000, 500)


def make_router(*providers, clock=None, **options):
    clock = clock or Clock()
    options.setdefault("budget_usd", 100.0)
    return Router({"svc": list(providers)}, clock=clock, sleep=clock.sleep, **options)


def test_healthy_primary_answers_alone():
    first, second = Scripted("a"), Scripted("b")
    result = make_router(first, second).complete("svc", "hi")
    assert (result.provider, result.attempts, result.fallback) == ("a", 1, False)
    assert second.calls == 0


def test_transient_failure_is_retried_on_same_provider():
    first = Scripted("a", [TransientError("503")])
    result = make_router(first, Scripted("b")).complete("svc", "hi")
    assert result.provider == "a"
    assert result.attempts == 2


def test_retries_exhausted_fall_back_to_next_provider():
    first = Scripted("a", [TransientError("503")] * 3)
    result = make_router(first, Scripted("b")).complete("svc", "hi")
    assert result.provider == "b"
    assert result.fallback is True
    assert first.calls == 3


def test_permanent_error_skips_retries():
    first = Scripted("a", [PermanentError("401"), PermanentError("401")])
    result = make_router(first, Scripted("b")).complete("svc", "hi")
    assert result.provider == "b"
    assert first.calls == 1


def test_backoff_grows_between_attempts():
    clock = Clock()
    start = clock.now
    first = Scripted("a", [TransientError("x")] * 3)
    make_router(first, Scripted("b"), clock=clock).complete("svc", "hi")
    assert clock.now - start == pytest.approx(0.5 + 1.0)


def test_all_providers_down_raises():
    first = Scripted("a", [TransientError("x")] * 3)
    second = Scripted("b", [PermanentError("y")])
    with pytest.raises(AllProvidersFailed):
        make_router(first, second).complete("svc", "hi")


def test_open_circuit_stops_calling_provider():
    clock = Clock()
    first = Scripted("a", [TransientError("x")] * 50)
    router = make_router(first, Scripted("b"), clock=clock, breaker_threshold=3)
    router.complete("svc", "1")
    calls = first.calls
    router.complete("svc", "2")
    assert calls == 3
    assert first.calls == calls


def test_half_open_probe_closes_circuit_after_recovery():
    clock = Clock()
    first = Scripted("a", [TransientError("x")] * 3)
    router = make_router(first, Scripted("b"), clock=clock,
                         breaker_threshold=3, breaker_cooldown=60)
    router.complete("svc", "1")
    clock.now += 61
    assert router.complete("svc", "2").provider == "a"
    assert router.breakers["a"].state == "closed"


def test_failed_probe_reopens_circuit():
    clock = Clock()
    breaker = CircuitBreaker(threshold=1, cooldown=10, clock=clock)
    breaker.record_failure()
    clock.now += 11
    assert breaker.state == "half_open"
    breaker.record_failure()
    assert breaker.state == "open"


def test_cost_uses_each_provider_price():
    result = make_router(Scripted("a", input_price=2, output_price=4)).complete("svc", "hi")
    assert result.cost_usd == pytest.approx((1000 * 2 + 500 * 4) / 1_000_000)


def test_budget_blocks_calls_and_resets_next_day():
    clock = Clock()
    provider = Scripted("a")
    router = make_router(provider, clock=clock, budget_usd=0.001)
    router.complete("svc", "1")
    with pytest.raises(BudgetExceeded):
        router.complete("svc", "2")
    assert provider.calls == 1
    clock.now += 86_400
    assert router.complete("svc", "3").provider == "a"


def test_services_keep_their_own_pinned_models():
    cheap, strong = Scripted("cheap"), Scripted("strong")
    clock = Clock()
    router = Router({"tagging": [cheap], "legal": [strong]}, budget_usd=10,
                    clock=clock, sleep=clock.sleep)
    assert router.complete("tagging", "x").model == "cheap-v1"
    assert router.complete("legal", "x").model == "strong-v1"


def test_audit_log_never_stores_the_prompt():
    events = []
    secret = "customer 123-45-6789"
    first = Scripted("a", [TransientError("503")])
    make_router(first, sink=events.append).complete("svc", secret)
    assert [event["event"] for event in events] == ["transient_failure", "success"]
    assert secret not in json.dumps(events)


@pytest.mark.parametrize("status, expected", [
    (429, TransientError), (500, TransientError), (503, TransientError),
    (400, PermanentError), (401, PermanentError), (404, PermanentError),
])
def test_http_status_classification(status, expected):
    assert classify_status(status) is expected


def test_http_provider_parses_response(monkeypatch):
    body = {"choices": [{"message": {"content": "hello"}}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 3}}

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr("urllib.request.urlopen",
                        lambda request, timeout: Response(json.dumps(body).encode()))
    provider = HttpProvider("p", "m", "https://example.test/v1", "key", 1, 1)
    assert provider.complete("hi") == Reply("hello", 7, 3)


def test_http_provider_maps_rate_limit_to_transient(monkeypatch):
    def raises(request, timeout):
        raise urllib.error.HTTPError("u", 429, "slow down", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", raises)
    provider = HttpProvider("p", "m", "https://example.test/v1", "key", 1, 1)
    with pytest.raises(TransientError):
        provider.complete("hi")
