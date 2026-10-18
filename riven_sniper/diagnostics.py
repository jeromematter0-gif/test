"""A single, verified HTTPS attempt with a shareable, credential-free report."""

from __future__ import annotations

import platform
from typing import Any

from .core import API_URL, VERSION, ApiFailure, MarketClient, Settings, safe_failure_details, utc_now


def connection_report(settings: Settings, *, client: Any = None) -> dict[str, Any]:
    client = client or MarketClient(settings)
    report: dict[str, Any] = {"version": VERSION, "at": utc_now(), "python": platform.python_version(),
                              "system": platform.system(), "endpoint": API_URL, "tls_verification": True}
    try:
        auctions, ms = client.search()
        report.update(ok=True, message="Réponse API valide reçue. La vitesse et la couverture ne sont pas validées par cet appel unique.",
                      returned_count=len(auctions), request_ms=ms)
    except ApiFailure as exc:
        report.update(ok=False, message=str(exc), error_kind=exc.kind, http_status=exc.http_status,
                      request_ms=exc.duration_ms, retry_after=exc.retry_after, details=safe_failure_details(exc))
    finally:
        client.close()
    return report
