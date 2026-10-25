import json
from datetime import timedelta
from decimal import Decimal

import httpx
import pytest
from bagel_market_data.domain import TimeRange, parse_boundary
from bagel_market_data.providers.alpaca import AlpacaREST, ProviderError, TransientProviderError

RAW = {
    "t": "2026-10-22T14:00:00Z",
    "o": 100,
    "h": 101,
    "l": 99,
    "c": 100.25,
    "v": 1000,
    "vw": 100.125,
    "n": 10,
}


def window():
    start = parse_boundary("2026-10-22T14:00:00Z")
    return TimeRange(start, start + timedelta(minutes=2))


def test_pagination_includes_later_symbols_and_precise_prices(settings):
    requests = []

    def handle(request):
        requests.append(request)
        if request.url.params.get("page_token"):
            return httpx.Response(200, json={"bars": {"VXUS": [RAW]}, "next_page_token": None})
        return httpx.Response(
            200,
            content=json.dumps(
                {"bars": {"SPY": [{**RAW, "vw": "100.1234567890"}]}, "next_page_token": "second"}
            ),
        )

    provider = AlpacaREST(settings, httpx.Client(transport=httpx.MockTransport(handle)))
    pages = list(provider.pages(["SPY", "VXUS"], window()))
    assert [b.symbol for page in pages for b in page] == ["SPY", "VXUS"]
    assert pages[0][0].vwap == Decimal("100.1234567890")
    assert requests[0].url.params["feed"] == "iex"
    assert requests[0].url.params["adjustment"] == "raw"
    assert requests[1].url.params["page_token"] == "second"
    assert requests[0].url.params["end"] == "2026-10-22T14:01:59.999999+00:00"
    assert requests[0].url.params["asof"] == provider.symbol_asof


def test_retry_after_and_auth_fail_fast(settings, monkeypatch):
    attempts = []
    sleeps = []
    monkeypatch.setattr(AlpacaREST, "_pause", lambda self, seconds: sleeps.append(seconds))

    def handle(request):
        attempts.append(request)
        return (
            httpx.Response(429, headers={"Retry-After": "3"})
            if len(attempts) == 1
            else (httpx.Response(200, json={"bars": {}, "next_page_token": None}))
        )

    provider = AlpacaREST(settings, httpx.Client(transport=httpx.MockTransport(handle)))
    assert list(provider.pages(["SPY"], window())) == [[]]
    assert 3 in sleeps and len(attempts) == 2
    provider = AlpacaREST(
        settings,
        httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(403, text="must never log this body")
            )
        ),
    )
    with pytest.raises(ProviderError, match="403") as exc:
        list(provider.pages(["SPY"], window()))
    assert "body" not in str(exc.value)


def test_retry_exhaustion_and_repeated_token(settings, monkeypatch):
    monkeypatch.setattr(AlpacaREST, "_pause", lambda self, seconds: None)
    provider = AlpacaREST(
        settings, httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(503)))
    )
    with pytest.raises(TransientProviderError):
        list(provider.pages(["SPY"], window()))
    provider = AlpacaREST(
        settings,
        httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json={"bars": {}, "next_page_token": "same"})
            )
        ),
    )
    with pytest.raises(ProviderError, match="Repeated"):
        list(provider.pages(["SPY"], window()))


def test_calendar_handles_holiday_and_early_close(settings):
    provider = AlpacaREST(
        settings,
        httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200, json=[{"date": "2026-11-27", "open": "09:30", "close": "13:00"}]
                )
            )
        ),
    )
    calendar = provider.calendar(
        parse_boundary("2026-11-26").date(), parse_boundary("2026-11-27").date()
    )
    assert len(calendar) == 1
    day = next(iter(calendar.values()))
    assert day.close.hour == 18  # 13:00 EST


def test_cancel_prevents_more_provider_requests(settings):
    def unexpected_request(_):
        pytest.fail("A cancelled provider must not issue another request")

    provider = AlpacaREST(settings, httpx.Client(transport=httpx.MockTransport(unexpected_request)))
    provider.cancel()
    with pytest.raises(TransientProviderError, match="cancelled"):
        list(provider.pages(["SPY"], window()))


def test_clock_uses_paper_endpoint_and_preserves_closed_state(settings):
    def handle(request):
        assert str(request.url) == "https://paper-api.alpaca.markets/v2/clock"
        return httpx.Response(
            200, json={"timestamp": "2026-10-24T12:00:00-04:00", "is_open": False}
        )

    clock = AlpacaREST(settings, httpx.Client(transport=httpx.MockTransport(handle))).clock()
    assert not clock.is_open and clock.timestamp.hour == 16
