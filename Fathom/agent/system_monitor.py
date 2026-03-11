"""
system_monitor.py — Collects safe, high-level system metrics from the local machine.

Collected data:
  • CPU usage %
  • RAM usage %
  • Disk usage % (primary disk)
  • System uptime in seconds
  • Static device info (hostname, OS, IP, username)

NO sensitive data is ever collected.
"""

import platform
import socket
import time
import logging
from datetime import datetime, timezone

import psutil

logger = logging.getLogger(__name__)

# Cache static device info so we only compute it once
_device_info_cache: dict | None = None


def get_device_info() -> dict:
    """
    Return static identification data about this machine.
    Safe fields only — no hardware serials, no MAC addresses, no passwords.
    """
    global _device_info_cache
    if _device_info_cache is not None:
        return _device_info_cache

    # Best-effort local IP (doesn't actually connect)
    ip = "unknown"
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
    except Exception:
        try:
            ip = socket.gethostbyname(socket.gethostname())
        except Exception:
            pass

    uname = platform.uname()
    _device_info_cache = {
        "hostname": socket.gethostname(),
        "os_version": f"{uname.system} {uname.release} ({uname.version[:40]})",
        "ip_address": ip,
        "username": _get_username(),
    }
    return _device_info_cache


def _get_username() -> str:
    try:
        import getpass
        return getpass.getuser()
    except Exception:
        return "unknown"


def get_system_metrics() -> dict:
    """
    Snapshot current system resource utilisation.
    Returns a dict safe for serialisation and transmission.
    """
    cpu = psutil.cpu_percent(interval=0.5)

    vm = psutil.virtual_memory()
    ram_pct = vm.percent

    # Use the root / or C:\ partition
    try:
        disk = psutil.disk_usage("/")
        disk_pct = disk.percent
    except Exception:
        try:
            disk = psutil.disk_usage("C:\\")
            disk_pct = disk.percent
        except Exception:
            disk_pct = 0.0

    uptime = int(time.time() - psutil.boot_time())

    return {
        "cpu_percent": round(cpu, 1),
        "ram_percent": round(ram_pct, 1),
        "disk_percent": round(disk_pct, 1),
        "uptime_seconds": uptime,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
