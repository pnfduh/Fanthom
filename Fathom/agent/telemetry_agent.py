"""
telemetry_agent.py — Lightweight endpoint telemetry agent for PC2.

Run from a USB flash drive:
    python telemetry_agent.py [--config path/to/config.json]

Features:
  • Reads server address from config.json (co-located on USB)
  • Sends telemetry every N seconds via WebSocket
  • Auto-reconnects with exponential backoff on disconnect
  • Buffers payloads locally (SQLite) when server is unreachable
  • Graceful shutdown on SIGINT / SIGTERM
"""

import argparse
import asyncio
import json
import logging
import os
import signal
import sqlite3
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

# ── Dependency check ──────────────────────────────────────────────────────────
try:
    import websockets
    import psutil
except ImportError as e:
    print(f"\n[ERROR] Missing dependency: {e}")
    print("Install requirements:  pip install -r requirements.txt")
    sys.exit(1)

from system_monitor import get_device_info, get_system_metrics
from activity_monitor import get_activity_metrics, start_input_listeners

# ── Logging setup ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("agent.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("telemetry_agent")

# ── Config defaults ───────────────────────────────────────────────────────────
DEFAULT_CONFIG = {
    "server_host": "192.168.1.100",
    "server_port": 8000,
    "poll_interval_seconds": 4,
    "reconnect_base_delay": 3,
    "reconnect_max_delay": 60,
    "buffer_max_size": 500,
    "device_id": None,   # auto-generated if null
}


def load_config(path: str) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    p = Path(path)
    if p.exists():
        try:
            with open(p, "r") as f:
                user_cfg = json.load(f)
            cfg.update(user_cfg)
            logger.info("Config loaded from %s", p)
        except Exception as exc:
            logger.warning("Could not parse config (%s) — using defaults", exc)
    else:
        logger.warning("Config file not found at %s — using defaults", p)
        # Write a default config so the user can edit it
        try:
            with open(p, "w") as f:
                json.dump(cfg, f, indent=2)
            logger.info("Default config written to %s", p)
        except Exception:
            pass

    # Generate / persist device ID
    if not cfg.get("device_id"):
        cfg["device_id"] = f"{_short_hostname()}-{uuid.uuid4().hex[:8]}"
        try:
            with open(p, "w") as f:
                json.dump(cfg, f, indent=2)
        except Exception:
            pass

    return cfg


def _short_hostname() -> str:
    try:
        import socket
        return socket.gethostname()[:16]
    except Exception:
        return "device"


# ── Local buffer (SQLite) ─────────────────────────────────────────────────────

class LocalBuffer:
    """SQLite-backed queue for telemetry payloads when server is unreachable."""

    def __init__(self, max_size: int = 500) -> None:
        self.max_size = max_size
        db_path = Path(__file__).parent / "buffer.db"
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS buffer (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                payload TEXT NOT NULL,
                ts      TEXT NOT NULL
            )
        """)
        self._conn.commit()

    def push(self, payload: dict) -> None:
        self._conn.execute(
            "INSERT INTO buffer (payload, ts) VALUES (?, ?)",
            (json.dumps(payload), datetime.now(timezone.utc).isoformat()),
        )
        self._conn.commit()
        # Trim if over max
        count = self._conn.execute("SELECT COUNT(*) FROM buffer").fetchone()[0]
        if count > self.max_size:
            self._conn.execute("""
                DELETE FROM buffer WHERE id IN (
                    SELECT id FROM buffer ORDER BY id ASC LIMIT ?
                )
            """, (count - self.max_size,))
            self._conn.commit()

    def pop_all(self) -> list[tuple[int, dict]]:
        rows = self._conn.execute(
            "SELECT id, payload FROM buffer ORDER BY id ASC"
        ).fetchall()
        return [(r[0], json.loads(r[1])) for r in rows]

    def delete(self, row_ids: list[int]) -> None:
        if not row_ids:
            return
        placeholders = ",".join("?" * len(row_ids))
        self._conn.execute(f"DELETE FROM buffer WHERE id IN ({placeholders})", row_ids)
        self._conn.commit()

    def size(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM buffer").fetchone()[0]


# ── Telemetry Agent ───────────────────────────────────────────────────────────

class TelemetryAgent:

    def __init__(self, config: dict) -> None:
        self.config = config
        self.device_id: str = config["device_id"]
        # Auto-detect wss:// for cloud hostnames, ws:// for local IPs
        host = config['server_host']
        port = config.get('server_port', 8000)
        is_cloud = (
            '.' in host and
            not host.replace('.', '').isdigit() and  # not a raw IP
            not host.startswith('192.') and
            not host.startswith('10.') and
            not host.startswith('172.') and
            not host in ('localhost', '127.0.0.1')
        )
        if is_cloud:
            # Cloudflare tunnel — use wss:// with no port
            self.server_url = f"wss://{host}/ws/agent/{self.device_id}"
        else:
            # Local network — use ws:// with port
            self.server_url = f"ws://{host}:{port}/ws/agent/{self.device_id}"
        self.poll_interval = config.get("poll_interval_seconds", 4)
        self.reconnect_base = config.get("reconnect_base_delay", 3)
        self.reconnect_max = config.get("reconnect_max_delay", 60)

        self.buffer = LocalBuffer(config.get("buffer_max_size", 500))
        self._stop_event = asyncio.Event()
        self._device_info = get_device_info()

        logger.info("Agent initialised")
        logger.info("  device_id   : %s", self.device_id)
        logger.info("  server URL  : %s", self.server_url)
        logger.info("  poll every  : %ds", self.poll_interval)

    def _build_payload(self) -> dict:
        return {
            "type": "telemetry",
            "device_info": self._device_info,
            "system": get_system_metrics(),
            "activity": get_activity_metrics(),
        }

    async def _flush_buffer(self, ws) -> None:
        """Send buffered payloads first, then delete them on success."""
        buffered = self.buffer.pop_all()
        if buffered:
            logger.info("Flushing %d buffered payloads…", len(buffered))
        sent_ids: list[int] = []
        for row_id, payload in buffered:
            try:
                await ws.send(json.dumps(payload))
                sent_ids.append(row_id)
                await asyncio.sleep(0.05)   # gentle pacing
            except Exception:
                break
        self.buffer.delete(sent_ids)
        if sent_ids:
            logger.info("Flushed %d buffered payloads", len(sent_ids))

    async def _run_connection(self) -> None:
        """Single WebSocket connection lifecycle."""
        async with websockets.connect(
            self.server_url,
            ping_interval=10,       # ping every 10s (was 20)
            ping_timeout=15,        # fail fast if no pong (was 30)
            close_timeout=5,        # don't hang on close (was 10)
            open_timeout=15,        # fail fast if can't connect
        ) as ws:
            logger.info("Connected to server ✓")
            await self._flush_buffer(ws)

            consecutive_errors = 0
            while not self._stop_event.is_set():
                try:
                    payload = self._build_payload()
                    await ws.send(json.dumps(payload))
                    consecutive_errors = 0
                    logger.debug(
                        "Sent  cpu=%.1f%%  ram=%.1f%%  idle=%.0fs",
                        payload["system"].get("cpu_percent", 0),
                        payload["system"].get("ram_percent", 0),
                        payload["activity"].get("idle_seconds", 0),
                    )
                except Exception as send_err:
                    consecutive_errors += 1
                    logger.warning("Send error #%d: %s", consecutive_errors, send_err)
                    if consecutive_errors >= 3:
                        raise   # force reconnect
                await asyncio.sleep(self.poll_interval)

    async def run(self) -> None:
        delay = self.reconnect_base

        while not self._stop_event.is_set():
            try:
                await self._run_connection()
            except (OSError, websockets.exceptions.WebSocketException) as exc:
                logger.warning("Connection lost: %s", exc)
                # Buffer current snapshot while offline
                try:
                    self.buffer.push(self._build_payload())
                except Exception:
                    pass
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.exception("Unexpected error: %s", exc)

            if self._stop_event.is_set():
                break

            logger.info("Reconnecting in %.0fs  (buffer size: %d)…", delay, self.buffer.size())
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=delay
                )
            except asyncio.TimeoutError:
                pass

            delay = min(delay * 1.5, self.reconnect_max)

    def stop(self) -> None:
        logger.info("Stopping agent…")
        self._stop_event.set()


# ── Entry point ───────────────────────────────────────────────────────────────

async def _main(config_path: str) -> None:
    config = load_config(config_path)
    agent = TelemetryAgent(config)

    # Start input rate listeners (keyboard / mouse EPM)
    start_input_listeners()

    # Register OS signal handlers for graceful shutdown
    loop = asyncio.get_event_loop()

    def _handle_signal():
        agent.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_signal)
        except NotImplementedError:
            # Windows — fall back to KeyboardInterrupt
            pass

    try:
        await agent.run()
    except KeyboardInterrupt:
        agent.stop()

    logger.info("Agent shut down cleanly.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Endpoint Telemetry Agent")
    parser.add_argument(
        "--config",
        default=str(Path(__file__).parent / "config.json"),
        help="Path to config.json",
    )
    args = parser.parse_args()
    asyncio.run(_main(args.config))
