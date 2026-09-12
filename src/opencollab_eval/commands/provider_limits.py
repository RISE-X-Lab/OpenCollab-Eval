"""Cross-process request slots, held until the actual upstream response closes."""

from __future__ import annotations

import fcntl
import json
import os
import threading
import time
import urllib.error
import urllib.parse
from pathlib import Path


class Cancelled(Exception):
    pass


class ProviderDisabled(ValueError):
    pass


class Lease:
    def __init__(self, fd):
        self.fd = fd
        self.lock = threading.Lock()

    def close(self):
        with self.lock:
            if self.fd is not None:
                os.close(self.fd)
                self.fd = None


class ProviderLimits:
    def __init__(self, config_path, *, disconnected=lambda: False):
        self.path = Path(config_path)
        self.disconnected = disconnected
        self.mutex = threading.Lock()
        self.waiting = {}
        self.peak = {}

    def config(self):
        value = json.loads(self.path.read_text())
        reserve = value.get("unconfirmed_previous_request_reserve", 0)
        if type(reserve) is not int or reserve < 0:
            raise ValueError("request reserve must be a non-negative integer")
        for name in value["providers"]:
            if type(value["providers"][name].get("enabled", True)) is not bool:
                raise ValueError("provider enabled must be a boolean")
            limit = value["providers"][name]["max_active"]
            if type(limit) is not int or not 1 <= limit <= 100:
                raise ValueError("invalid provider request limit")
        return value

    @staticmethod
    def alive(item):
        try:
            fields = Path(f"/proc/{item['pid']}/stat").read_text().rsplit(")", 1)[1].split()
            return fields[0] not in {"Z", "X"} and fields[19] == str(item["start_ticks"])
        except (OSError, KeyError, IndexError):
            return False

    def reserved(self, cfg):
        total = int(cfg.get("unconfirmed_previous_request_reserve", 0))
        for item in cfg.get("legacy_services", []):
            if self.alive(item):
                limit = item["max_active"]
                try:
                    limit = max(limit, int(json.loads(Path(item["config_path"]).read_text())["max_active"]))
                except (OSError, ValueError, KeyError):
                    pass
                total += limit
        return total

    def directory(self, cfg, name):
        root = Path(cfg["lock_root"]) / name
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        return root

    @staticmethod
    def scan(root, *, keep_one=False):
        active = []
        free_fd = None
        try:
            for index in range(100):
                fd = os.open(root / f"{index:02d}.slot", os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
                try:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        try:
                            meta = json.loads(os.read(fd, 4096))
                        except (ValueError, OSError):
                            meta = {}
                        active.append(meta)
                    else:
                        if keep_one and free_fd is None:
                            free_fd, fd = fd, None
                finally:
                    if fd is not None:
                        os.close(fd)
        except BaseException:
            if free_fd is not None:
                os.close(free_fd)
            raise
        return active, free_fd

    def acquire(self, url, cancelled=lambda: False):
        cfg = self.config()
        parsed = urllib.parse.urlsplit(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        name = next((n for n, p in cfg["providers"].items() if p["origin"] == origin), None)
        if name is None:
            raise ValueError("unconfigured upstream origin")
        root = self.directory(cfg, name)
        with self.mutex:
            self.waiting[name] = self.waiting.get(name, 0) + 1
        try:
            while True:
                if cancelled() or self.disconnected():
                    raise Cancelled()
                cfg = self.config()
                if cfg["providers"][name].get("enabled", True) is False:
                    raise ProviderDisabled(name)
                limit = cfg["providers"][name]["max_active"]
                reserve = self.reserved(cfg)
                with (root / "allocation.lock").open("a") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX)
                    active, fd = self.scan(root, keep_one=True)
                    if fd is not None and len(active) + reserve < limit:
                        meta = {
                            "pid": os.getpid(),
                            "thread": threading.get_ident(),
                            "provider": name,
                            "started_epoch": time.time(),
                        }
                        try:
                            os.ftruncate(fd, 0)
                            os.lseek(fd, 0, os.SEEK_SET)
                            payload = json.dumps(meta).encode()
                            if os.write(fd, payload) != len(payload):
                                raise OSError("short provider lease metadata write")
                        except BaseException:
                            os.close(fd)
                            raise
                        self.peak[name] = max(self.peak.get(name, 0), len(active) + 1)
                        return Lease(fd)
                    if fd is not None:
                        os.close(fd)
                time.sleep(0.25)
        finally:
            with self.mutex:
                self.waiting[name] -= 1

    def snapshot(self):
        cfg = self.config()
        providers = {}
        reserve = self.reserved(cfg)
        for name, policy in cfg["providers"].items():
            root = self.directory(cfg, name)
            with (root / "allocation.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                active, _ = self.scan(root)
            providers[name] = {
                "name": policy["name"],
                "enabled": policy.get("enabled", True),
                "max_active": policy["max_active"],
                "active": len(active),
                "service_active": sum(x.get("pid") == os.getpid() for x in active),
                "service_waiting": self.waiting.get(name, 0),
                "reserved_for_legacy": reserve,
                "available_for_new": max(0, policy["max_active"] - reserve - len(active))
                if policy.get("enabled", True)
                else 0,
            }
        return {
            "mode": "per_provider_cross_process",
            "providers": providers,
            "configured_total": sum(p["max_active"] for p in cfg["providers"].values()),
            "service_active": sum(p["service_active"] for p in providers.values()),
            "service_waiting": sum(p["service_waiting"] for p in providers.values()),
        }


class LeasedResponse:
    """Release a request slot when its owned HTTP response closes."""

    def __init__(self, response, release):
        self.response = response
        self.release = release

    def __getattr__(self, name):
        return getattr(self.response, name)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        try:
            self.response.close()
        finally:
            self.release()


def install(module, config_path):
    existing = getattr(module, "_upstream_provider_limits", None)
    if existing is not None:
        if existing.path != Path(config_path):
            raise ValueError("provider limits already configured")
        return existing
    limiter = ProviderLimits(config_path)

    def wrap(original):
        def open_limited(request, client, timeout, **kwargs):
            try:
                lease = limiter.acquire(request.full_url, lambda: module._client_disconnected(client))
            except Cancelled as exc:
                raise module._ClientDisconnected() from exc
            try:
                try:
                    response = original(request, client, timeout, **kwargs)
                except urllib.error.HTTPError as error_response:
                    response = error_response
                return LeasedResponse(response, lease.close)
            except BaseException:
                lease.close()
                raise

        return open_limited

    for name in ("_open_direct_upstream", "_open_default_upstream"):
        original = getattr(module, name, None)
        if original is not None:
            setattr(module, name, wrap(original))
    module._upstream_provider_limits = limiter
    return limiter
