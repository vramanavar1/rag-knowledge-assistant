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

Concurrency
-----------
The client is ``httpx.AsyncClient``.  Every Azure call in this codebase is
awaited, so a request that is waiting on the network occupies a coroutine rather
than a thread -- which is what lets one replica hold thousands of in-flight
questions instead of the ~40 that a threadpool allows.

The connection pool is the outermost back-pressure valve.  It is deliberately
finite: ``asyncio.gather`` over a few million embedding batches would otherwise
try to open all of them at once, and the first thing to break would be the
Azure OpenAI quota rather than anything this process could see.
"""

from __future__ import annotations

import asyncio
import ssl
from pathlib import Path
from typing import Any

import httpx

from rag.config import Settings
from rag.observability.tracing import get_logger

log = get_logger(__name__)

RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}

# Pool bounds, overridable with AZURE_HTTP_MAX_CONNECTIONS.
#
# Sizing matters more than it looks. One question fans out to as many as three
# concurrent searches, so a replica holding N questions wants ~3N connections.
# Set it too low and the pool becomes the bottleneck -- and worse, the readiness
# probe's own call queues behind query traffic, so the orchestrator sees an
# unhealthy replica exactly when it is busiest. Measured at 64 connections under
# 60 concurrent questions: /health p50 297ms.
DEFAULT_MAX_CONNECTIONS = 200

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


def make_client(settings: Settings, timeout_s: float = 60.0) -> httpx.AsyncClient:
    """An async client.

    Safe to construct outside a running event loop -- httpx defers creating the
    connection pool's loop-bound state until the first request -- which is what
    lets providers keep building their client in ``__init__``.
    """
    return httpx.AsyncClient(
        timeout=httpx.Timeout(timeout_s, connect=10.0),
        verify=build_verify(settings),
        limits=httpx.Limits(
            max_connections=settings.http_max_connections,
            max_keepalive_connections=max(8, settings.http_max_connections // 4),
        ),
    )


async def post_with_retry(
    client: httpx.AsyncClient,
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    *,
    what: str = "request",
    max_attempts: int = 4,
) -> httpx.Response:
    """POST with backoff on throttling. Raises on permanent failure.

    The backoff is ``asyncio.sleep``, so a throttled call yields the event loop
    to every other in-flight request instead of parking a thread on it.
    """
    last_error = ""

    for attempt in range(max_attempts):
        try:
            response = await client.post(url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt == max_attempts - 1:
                break
            await asyncio.sleep(min(2 ** attempt, 8))
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
            await asyncio.sleep(delay)
            continue

        raise RuntimeError(
            f"{what} failed with {response.status_code}: {response.text[:300]}"
        )

    raise RuntimeError(f"{what} unreachable: {last_error}")


async def aclose(*clients: httpx.AsyncClient | None) -> None:
    """Release connection pools. An unclosed AsyncClient leaks its sockets."""
    for client in clients:
        if client is None:
            continue
        try:
            await client.aclose()
        except Exception:                                       # noqa: BLE001
            # Shutdown must not raise; a half-closed pool is the OS's problem.
            log.debug("error closing http client", exc_info=True)
