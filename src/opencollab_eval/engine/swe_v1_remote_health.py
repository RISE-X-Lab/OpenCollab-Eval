"""Bounded HTTP health probing for the remote evaluation adapter."""

from __future__ import annotations

import math
import time
import urllib.error
import urllib.request


def http_health(url: str, timeout: object = 15) -> dict[str, object]:
    """Probe a service with a finite end-to-end budget and transient retries."""
    try:
        budget = float(timeout)
    except (TypeError, ValueError, OverflowError):
        return {"ok": False, "error": "health timeout must be finite and positive"}
    if isinstance(timeout, bool) or not math.isfinite(budget) or budget <= 0:
        return {"ok": False, "error": "health timeout must be finite and positive"}
    deadline = time.monotonic() + budget
    last: dict[str, object] | None = None
    for attempt in range(3):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            with urllib.request.urlopen(url, timeout=remaining) as response:
                body = response.read(200).decode("utf-8", errors="replace")
                result = {
                    "ok": 200 <= response.status < 400,
                    "status": response.status,
                    "body": body,
                }
                if result["ok"] or response.status < 500 or attempt == 2:
                    return result
                last = result
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            try:
                body = exc.read(200).decode("utf-8", errors="replace")
            except (OSError, UnicodeError):
                body = ""
            if status < 500:
                return {"ok": False, "status": status, "body": body}
            last = {"ok": False, "status": status, "body": body}
        except Exception as exc:
            last = {"ok": False, "error": str(exc)[:500]}
        remaining = deadline - time.monotonic()
        if attempt < 2 and remaining > 0:
            time.sleep(min(0.2, remaining))
    return last or {"ok": False, "error": "health probe timed out"}


__all__ = ["http_health"]
