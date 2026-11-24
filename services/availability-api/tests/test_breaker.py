from app.breaker import CLOSED, HALF_OPEN, OPEN, CircuitBreaker


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def make(threshold=3, reset=10.0):
    clock = FakeClock()
    return CircuitBreaker(threshold, reset, clock=clock), clock


def test_opens_after_consecutive_failures():
    b, _ = make()
    for _ in range(2):
        assert b.allow()
        b.record_failure()
    assert b.state == CLOSED
    b.record_failure()
    assert b.state == OPEN
    assert not b.allow()


def test_success_resets_failure_count():
    b, _ = make()
    b.record_failure()
    b.record_failure()
    b.record_success()
    b.record_failure()
    b.record_failure()
    assert b.state == CLOSED


def test_half_open_allows_exactly_one_trial():
    b, clock = make()
    for _ in range(3):
        b.record_failure()
    clock.now = 10.0
    assert b.state == HALF_OPEN
    assert b.allow()
    assert not b.allow()  # second caller is still refused while the trial runs


def test_trial_success_closes():
    b, clock = make()
    for _ in range(3):
        b.record_failure()
    clock.now = 10.0
    b.allow()
    b.record_success()
    assert b.state == CLOSED
    assert b.allow()


def test_trial_failure_reopens_for_full_timeout():
    b, clock = make()
    for _ in range(3):
        b.record_failure()
    clock.now = 10.0
    b.allow()
    b.record_failure()
    assert b.state == OPEN
    clock.now = 19.9
    assert not b.allow()
    clock.now = 20.0
    assert b.allow()


def test_straggler_success_does_not_close_open_breaker():
    b, _ = make()
    for _ in range(3):
        b.record_failure()
    b.record_success()  # a slow call that started before the breaker opened
    assert b.state == OPEN
