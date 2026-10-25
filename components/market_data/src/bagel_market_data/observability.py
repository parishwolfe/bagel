"""JSON logs, Prometheus metrics, and private container probes."""

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    generate_latest,
)


def configure_logging(level: str) -> None:
    class JsonFormatter(logging.Formatter):
        def format(self, record):
            payload = {
                "timestamp": self.formatTime(record),
                "severity": record.levelname,
                "event": record.getMessage(),
                "logger": record.name,
            }
            for name in (
                "attempt",
                "start",
                "end",
                "bars",
                "run_id",
                "error_type",
                "seconds",
                "years",
                "reason",
                "next_start",
                "feed",
                "connected",
                "session_active",
                "database_ok",
            ):
                if hasattr(record, name):
                    payload[name] = getattr(record, name)
            return json.dumps(payload)

    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=level, handlers=[handler], force=True)
    # These transports can log authentication payloads at DEBUG.
    for name in ("httpx", "httpcore", "websockets", "alpaca"):
        logging.getLogger(name).setLevel(logging.WARNING)
    # Pool connection diagnostics may include raw libpq errors. Application retries
    # emit only safe error class names instead.
    logging.getLogger("psycopg").setLevel(logging.CRITICAL)
    logging.getLogger("psycopg.pool").setLevel(logging.CRITICAL)


@dataclass
class Health:
    tick: float = field(default_factory=time.monotonic)
    database_ok: bool = False
    connected: bool = False
    session_active: bool = False
    stopping: bool = False

    @property
    def live(self) -> bool:
        return not self.stopping and time.monotonic() - self.tick < 60

    @property
    def ready(self) -> bool:
        return self.live and self.database_ok and (not self.session_active or self.connected)


class Metrics:
    def __init__(self):
        self.registry = CollectorRegistry()
        self.connected = Gauge(
            "bagel_stream_connected", "Authenticated stream", registry=self.registry
        )
        self.received = Counter(
            "bagel_bars_received", "Stream observations", registry=self.registry
        )
        self.written = Counter(
            "bagel_bars_written", "Committed observations", registry=self.registry
        )
        self.write_failures = Counter(
            "bagel_write_failures", "Failed batches", registry=self.registry
        )
        self.reconnects = Counter(
            "bagel_stream_reconnects", "Reconnect attempts", registry=self.registry
        )
        self.queue_depth = Gauge(
            "bagel_queue_depth", "Buffered observations", registry=self.registry
        )
        self.lag = Gauge(
            "bagel_ingestion_lag_seconds", "Last persisted bar age", registry=self.registry
        )
        self.repair_backlog = Gauge(
            "bagel_repair_backlog_seconds", "Unrepaired time", registry=self.registry
        )
        self.pending = Gauge(
            "bagel_pending_ranges", "Durable pending ranges", registry=self.registry
        )
        self.last_reconcile = Gauge(
            "bagel_last_reconciliation_timestamp_seconds",
            "Last successful reconciliation",
            registry=self.registry,
        )


def serve_health(health: Health, metrics: Metrics, port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/metrics":
                self.send_response(200)
                self.send_header("Content-Type", CONTENT_TYPE_LATEST)
                self.end_headers()
                self.wfile.write(generate_latest(metrics.registry))
                return
            if self.path not in ("/health/live", "/health/ready"):
                self.send_error(404)
                return
            ok = health.live if self.path == "/health/live" else health.ready
            self.send_response(200 if ok else 503)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps(
                    {
                        "ok": ok,
                        "connected": health.connected,
                        "session_active": health.session_active,
                    }
                ).encode()
            )

        def log_message(self, format: str, *args):
            pass

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True, name="bagel-health").start()
    return server
