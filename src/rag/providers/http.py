"""Shared HTTP plumbing for the Azure REST calls: TLS policy and retries.

TLS
---
Corporate networks that inspect TLS present their own CA, and Python's bundled
certificate store does not know it -- which surfaces as
``CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate`` on every
Azure call while the same request succeeds from a browser.  Three escape
hatches, in precedence order:

``AZURE_CA_BUNDLE=/path/to/corp-root.pem``   the correct fix: trust the proxy's
                                             root CA explicitly.
``AZURE_USE_SYSTEM_CERTS=true``              load the OS trust store, which on
                                             Windows already contains the
                                             corporate root.
``AZURE_TLS_VERIFY=false``                   development only.  Logged loudly on
                                             every start.  Never set this on a
                                             deployed environment: it disables
                                             the check that the endpoint you are
                                             sending an API key to is really
                                             Azure.

Retries
-------
429 and 5xx are retried with exponential backoff, honouring ``Retry-After``.
Everything else fails fast, because retrying a 401 or a 404 just multiplies the
latency of a misconfiguration.
"""

from __future__ import annotations

import ssl
import time
from pathlib import Path
from typing import Any

import httpx

from rag.config import Settings
from rag.observability.tracing import get_logger

log = get_logger(__name__)

RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}
_warned_insecure = False


def build_verify(settings: Settings) -> bool | str | ssl.SSLContext:
    global _warned_insecure

    if settings.azure_ca_bundle:
        bundle = Path(settings.azure_ca_bundle)
        if bundle.exists():
            log.info("using custom CA bundle for Azure TLS", path=str(bundle))
            return str(bundle)
        log.warning("AZURE_CA_BUNDLE does not exist, ignoring",
                    path=settings.azure_ca_bundle)

    if settings.azure_use_system_certs:
        context = ssl.create_default_context()
        context.load_default_certs(ssl.Purpose.SERVER_AUTH)
        log.info("using the OS trust store for Azure TLS",
                 authorities=len(context.get_ca_certs()))
        return context

    if not settings.azure_tls_verify:
        if not _warned_insecure:
            log.warning(
                "TLS VERIFICATION DISABLED for Azure calls (AZURE_TLS_VERIFY=false). "
                "This is a development-only workaround for an intercepting proxy. "
                "Never use it outside a local machine.",
            )
            _warned_insecure = True
        return False

    return True


def make_client(settings: Settings, timeout_s: float = 60.0) -> httpx.Client:
    return httpx.Client(
        timeout=httpx.Timeout(timeout_s, connect=10.0),
        verify=build_verify(settings),
    )


def post_with_retry(
    client: httpx.Client,
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    *,
    what: str = "request",
    max_attempts: int = 4,
) -> httpx.Response:
    """POST with backoff on throttling. Raises on permanent failure."""
    last_error = ""

    for attempt in range(max_attempts):
        try:
            response = client.post(url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt == max_attempts - 1:
                break
            time.sleep(min(2 ** attempt, 8))
            continue

        if response.status_code == 200:
            return response

        if response.status_code in RETRYABLE_STATUS:
            delay = float(response.headers.get("retry-after", min(2 ** attempt, 8)))
            log.warning(f"{what} throttled", status=response.status_code,
                        retry_in=delay, attempt=attempt + 1)
            if attempt == max_attempts - 1:
                last_error = f"{response.status_code} after {max_attempts} attempts"
                break
            time.sleep(delay)
            continue

        raise RuntimeError(
            f"{what} failed with {response.status_code}: {response.text[:300]}"
        )

    raise RuntimeError(f"{what} unreachable: {last_error}")
