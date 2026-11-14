"""Redis-first market bus. No database imports or calls belong in this module."""

import json
import logging
from datetime import UTC, datetime
from typing import Any, cast

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from redis import Redis
from redis.exceptions import ResponseError

from market.events.contracts import MarketEvent

log = logging.getLogger(__name__)
PERSISTENCE_GROUP = "market-persistence-v2"
DECISION_GROUP = "strategy-persistence-v2"


class BusSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", populate_by_name=True, hide_input_in_errors=True)
    redis_url: str = Field(default="redis://redis:6379/0", validation_alias="BAGEL_REDIS_URL")
    market_stream: str = Field(default="bagel:market:v2", validation_alias="BAGEL_MARKET_STREAM")
    decision_stream: str = Field(default="bagel:strategy:v2", validation_alias="BAGEL_DECISION_STREAM")
    stream_max_length: int = Field(default=200000, ge=1000, validation_alias="BAGEL_STREAM_MAX_LENGTH")


class MarketBus:
    def __init__(self, settings: BusSettings, client: Redis | None = None):
        self.settings = settings
        self.redis = client or Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_timeout=5,
            socket_connect_timeout=3,
            max_connections=8,
        )
        self._publisher_ready = False

    @classmethod
    def from_settings(cls, settings: Any = None) -> "MarketBus":
        config = (
            BusSettings()
            if settings is None
            else BusSettings(
                **{
                    name: getattr(settings, name)
                    for name in BusSettings.model_fields
                    if hasattr(settings, name)
                }
            )
        )
        return cls(config)

    def close(self) -> None:
        self.redis.close()

    def group(self, name: str, *, stream: str | None = None, start: str = "0-0") -> None:
        try:
            self.redis.xgroup_create(stream or self.settings.market_stream, name, id=start, mkstream=True)
        except ResponseError as error:
            if not str(error).startswith("BUSYGROUP"):
                raise

    def deliveries(self, group: str, owner: str, *, stream: str | None = None, count: int = 500) -> list:
        stream = stream or self.settings.market_stream
        owned = cast(list, self.redis.xreadgroup(group, owner, {stream: "0-0"}, count=count))
        if owned and owned[0][1]:
            return owned[0][1]
        reclaimed = cast(list, self.redis.xautoclaim(stream, group, owner, 60000, start_id="0-0", count=count))
        if reclaimed[2]:
            raise RuntimeError("Pending Redis events disappeared; recovery review required")
        if reclaimed[1]:
            return reclaimed[1]
        result = cast(list, self.redis.xreadgroup(group, owner, {stream: ">"}, count=count))
        return result[0][1] if result else []

    def publish(self, event: MarketEvent) -> str:
        if not self._publisher_ready:
            self.group(PERSISTENCE_GROUP)
            self._publisher_ready = True
        key = (
            self.settings.market_stream
            + ":latest:"
            + event.provider
            + ":"
            + event.feed
            + ":"
            + event.provider_symbol
        )
        script = """
        local old = redis.call('GET',KEYS[1])
        local replace = true
        if old then
          local p=cjson.decode(old)
          if p.order and p.order > ARGV[2] then replace = false end
        end
        local id = redis.call('XADD',KEYS[2],'*','event',ARGV[3])
        if replace then redis.call('SET',KEYS[1],ARGV[1]) end
        return id
        """
        rank = {"historical": 30, "live_updated": 20, "live": 10}[event.origin]
        order = ":".join(
            (
                f"{int(event.payload.start.timestamp() * 1000000):020d}",
                f"{rank:02d}",
                f"{int(event.observed_at.timestamp() * 1000000):020d}",
                f"{int(event.received_at.timestamp() * 1000000):020d}",
            )
        )
        encoded = json.dumps({"order": order, "event": event.model_dump(mode="json")})
        identity = cast(
            str,
            self.redis.eval(
                script, 2, key, self.settings.market_stream, encoded, order, event.model_dump_json()
            ),
        )
        log.info(
            "market_event_published",
            extra={
                "event_id": str(event.event_id),
                "stream_id": identity,
                "provider": event.provider,
                "feed": event.feed,
            },
        )
        return identity

    def latest(self, provider: str, feed: str, symbol: str) -> MarketEvent | None:
        raw = cast(
            str | None,
            self.redis.get(self.settings.market_stream + ":latest:" + provider + ":" + feed + ":" + symbol),
        )
        if raw is None:
            return None
        decoded = json.loads(raw)
        return MarketEvent.model_validate(decoded.get("event", decoded))

    def safe_trim(self, *, stream: str | None = None) -> int:
        """Redis 7.4 lacks ACKED trimming. Never trim ahead of any group's PEL."""
        stream = stream or self.settings.market_stream
        required = DECISION_GROUP if stream == self.settings.decision_stream else PERSISTENCE_GROUP
        # Group creation, ACK and reclaim cannot interleave with the retention decision.
        script = """
        if redis.call('XLEN',KEYS[1]) <= tonumber(ARGV[1]) then return 0 end
        local groups = redis.call('XINFO','GROUPS',KEYS[1])
        local floor = nil
        local protected = false
        local function before(a,b)
          local am,as = string.match(a,'(%d+)%-(%d+)')
          local bm,bs = string.match(b,'(%d+)%-(%d+)')
          return tonumber(am) < tonumber(bm) or
            (tonumber(am) == tonumber(bm) and tonumber(as) < tonumber(bs))
        end
        for _,entry in ipairs(groups) do
          local g = {}
          for i=1,#entry,2 do g[entry[i]] = entry[i+1] end
          if g.name == ARGV[2] then protected = true end
          local pending = redis.call('XPENDING',KEYS[1],g.name)
          local candidate = g['last-delivered-id']
          if pending[1] > 0 then candidate = pending[2] end
          if not floor or before(candidate,floor) then floor = candidate end
        end
        if not protected or not floor then return 0 end
        return redis.call('XTRIM',KEYS[1],'MINID',floor)
        """
        return cast(int, self.redis.eval(script, 1, stream, self.settings.stream_max_length, required))

    def commit_actor(
        self,
        state_key: str,
        lease_key: str,
        token: str,
        previous: str | None,
        state: dict,
        group: str,
        deliveries: list,
        emission: str | None,
    ) -> bool:
        """Fence state/emission/ACK, stopping before ACK on any Redis write error."""
        self.group(DECISION_GROUP, stream=self.settings.decision_stream)
        script = """
        if redis.call('GET',KEYS[1]) ~= ARGV[1] then return 0 end
        local old = redis.call('GET',KEYS[2]) or ''
        if old ~= ARGV[2] then return 0 end
        -- Redis has no transaction rollback. A failed emission must not ACK input.
        if ARGV[4] ~= '' then redis.call('XADD',KEYS[3],'*','signal',ARGV[4]) end
        redis.call('SET',KEYS[2],ARGV[3])
        if #ARGV > 5 then redis.call('XACK',KEYS[4],ARGV[5],unpack(ARGV,6)) end
        return 1
        """
        return bool(
            self.redis.eval(
                script,
                4,
                lease_key,
                state_key,
                self.settings.decision_stream,
                self.settings.market_stream,
                token,
                previous or "",
                json.dumps(state),
                emission or "",
                group,
                *[identity for identity, _ in deliveries],
            )
        )

    def snapshot(self) -> dict:
        memory = cast(dict, self.redis.info("memory"))
        persistence = cast(dict, self.redis.info("persistence"))
        streams = []
        for role, stream in (
            ("market", self.settings.market_stream),
            ("decisions", self.settings.decision_stream),
        ):
            exists = self.redis.exists(stream)
            streams.append(
                dict(
                    role=role,
                    length=cast(int, self.redis.xlen(stream)),
                    groups=cast(list, self.redis.xinfo_groups(stream)) if exists else [],
                )
            )
        return dict(
            used_memory=memory["used_memory"],
            maxmemory=memory["maxmemory"],
            aof_enabled=persistence["aof_enabled"],
            aof_write_ok=persistence["aof_last_write_status"] == "ok",
            streams=streams,
        )

    def heartbeat(self, role: str, process_id: str, status: str, details: dict) -> None:
        self.redis.set(
            self.settings.market_stream + ":worker:" + role,
            json.dumps(
                dict(
                    worker=role,
                    process_id=process_id,
                    status=status,
                    details=details,
                    updated_at=datetime.now(UTC).isoformat(),
                )
            ),
            ex=90,
        )

    def workers(self) -> list[dict]:
        rows = []
        for key in self.redis.scan_iter(match=self.settings.market_stream + ":worker:*"):
            raw = cast(str | None, self.redis.get(key))
            if raw:
                row = json.loads(raw)
                row["updated_at"] = datetime.fromisoformat(row["updated_at"])
                row["live"] = (datetime.now(UTC) - row["updated_at"]).total_seconds() < 30
                rows.append(row)
        return rows
