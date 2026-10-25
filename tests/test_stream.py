import json

import pytest
from bagel_market_data.domain import Origin
from bagel_market_data.providers.alpaca import AlpacaStream, ProviderError, TransientProviderError
from test_alpaca import RAW


class FakeSocket:
    def __init__(self, frames):
        self.frames = iter(frames)
        self.sent = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    async def recv(self):
        return json.dumps(next(self.frames))

    async def send(self, value):
        self.sent.append(json.loads(value))

    def __aiter__(self):
        return self

    async def __anext__(self):
        frame = next(self.frames, None)
        if frame is None:
            raise StopAsyncIteration
        return json.dumps(frame)


@pytest.mark.asyncio
async def test_updated_bars_and_disconnect_are_observable(settings):
    socket = FakeSocket(
        [
            [{"T": "success", "msg": "connected"}],
            [{"T": "success", "msg": "authenticated"}],
            [{"T": "subscription", "bars": ["SPY"], "updatedBars": ["SPY"]}],
            [{**RAW, "T": "b", "S": "SPY"}, {**RAW, "T": "u", "S": "SPY"}],
        ]
    )
    connections = []
    stream = AlpacaStream(settings, connections.append, lambda *_, **__: socket)
    received = []
    with pytest.raises(TransientProviderError):
        async for event in stream.events(["SPY"]):
            received.append(event)
    assert [origin for _, origin in received] == [Origin.LIVE, Origin.UPDATED]
    assert connections == [True, False]
    assert socket.sent[1]["updatedBars"] == ["SPY"]


@pytest.mark.asyncio
@pytest.mark.parametrize("code,expected", [(402, ProviderError), (409, TransientProviderError)])
async def test_stream_errors_are_not_silently_retried(settings, code, expected):
    socket = FakeSocket(
        [[{"T": "success", "msg": "connected"}], [{"T": "error", "code": code, "msg": "sensitive"}]]
    )
    stream = AlpacaStream(settings, connect_factory=lambda *_, **__: socket)
    with pytest.raises(expected) as exc:
        await anext(stream.events(["SPY"]))
    assert "sensitive" not in str(exc.value)
