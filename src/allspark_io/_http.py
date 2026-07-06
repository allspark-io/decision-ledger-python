"""Thin stdlib-only HTTP helper. No third-party HTTP client dependency —
see the package's zero-dependency design note in pyproject.toml/README."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass


@dataclass
class HttpError(Exception):
    """Raised for any failure — timeout, connection refused, non-2xx status.
    Callers that need fail-open behavior (POST /check) catch this broadly;
    callers that need durable retry (POST /events) let it propagate to the
    outbox worker, which retries later."""
    message: str
    status: int | None = None

    def __str__(self) -> str:
        return self.message


def post_json(url: str, payload: dict, *, timeout: float) -> dict:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", errors="replace")
        except OSError:
            # Reading the error body is itself a network operation — a
            # dropped connection here must not escape as a *different*,
            # uncaught exception type than the HttpError callers expect.
            detail = "<no response body>"
        raise HttpError(f"HTTP {exc.code} from {url}: {detail}", status=exc.code) from exc
    except urllib.error.URLError as exc:
        raise HttpError(f"could not reach {url}: {exc.reason}") from exc
    except TimeoutError as exc:
        raise HttpError(f"timed out reaching {url}") from exc
