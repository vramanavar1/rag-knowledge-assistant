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

Failure vocabulary
------------------
Every permanent failure -- from any Azure endpoint, not just this module's two
callers -- is raised as `AzureEndpointError`, which says what *this process
observed* rather than repeating what the service guessed the cause was:

    unreachable     no HTTP response at all: DNS, TLS, connect, timeout
    not_accessible  401/403 -- we reached it and it refused us
    not_found       404 -- no such deployment or index here
    unavailable     retryable status, retries exhausted

That distinction is not cosmetic.  Azure OpenAI answers a data-plane access-rule
rejection with the fixed sentence "Access denied due to Virtual Network/Firewall
rules." *whether or not a virtual network is involved* -- it is the one message
`networkAcls` and `publicNetworkAccess` share.  Pasting that into our own error
text once cost a day of looking for a VNet that did not exist, on a Container
Apps deployment that has none.  So the service's own words are kept, but demoted
to the `azure_message` field: still there to quote at support or match against
the portal, no longer the first thing an operator reads.

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

# How much of the service's response body to carry on the error. Generous on
# purpose: the body holds the `code` ("DeploymentNotFound", "invalid_api_key")
# that *is* the diagnosis. Safe to widen -- credentials travel as headers and
# never appear in a response body.
BODY_BUDGET = 700

# Bodies an access-rule rejection produces, across services. Matched case-
# insensitively against the raw text, which is the only place these words are
# allowed to appear.
_ACCESS_RULE_MARKERS = (
    "access denied",
    "firewall",
    "virtual network",
    "not allowed to access",
    "public network access",
    "forbidden by",
)


class AzureEndpointError(RuntimeError):
    """A permanent failure talking to an Azure endpoint.

    Subclasses `RuntimeError` deliberately: every existing handler catches that,
    so adding this type changed no control flow anywhere.  Read `category` to
    branch on the kind of failure; read `azure_message` for the service's own
    words, which are never interpolated into `str(self)`.
    """

    def __init__(
        self,
        service: str,
        *,
        category: str,
        hint: str,
        status: int | None = None,
        azure_message: str = "",
        endpoint: str = "",
        attempts: int = 0,
    ) -> None:
        self.service = service
        self.category = category
        self.hint = hint
        self.status = status
        self.azure_message = azure_message
        self.endpoint = endpoint
        self.attempts = attempts
        super().__init__(f"{headline(service, category, status, attempts)}: {hint}")

    def log_fields(self) -> dict[str, Any]:
        """The structured fields every caller logs, so they all log the same set."""
        return {
            "category": self.category,
            "status": self.status,
            "hint": self.hint,
            # The service's own text, quarantined to one field.
            "azure_message": self.azure_message,
        }


def headline(service: str, category: str, status: int | None,
             attempts: int = 0) -> str:
    """One line naming what happened, in our vocabulary rather than Azure's."""
    if category == "unreachable":
        return f"{service} endpoint unreachable"
    if category == "unavailable":
        suffix = f" after {attempts} attempts" if attempts else ""
        return f"{service} endpoint unavailable{suffix} (HTTP {status})"
    if category == "not_found":
        return f"{service} endpoint or deployment not found (HTTP {status})"
    return f"{service} endpoint not accessible (HTTP {status})"


def classify(status: int, body: str) -> tuple[str, str]:
    """Map an HTTP status and body to (category, hint).

    Pure, so `scripts/verify_azure_errors.py` can check the wording without a
    network or an Azure subscription.
    """
    lowered = body.lower()

    if status == 403:
        if any(marker in lowered for marker in _ACCESS_RULE_MARKERS):
            return "not_accessible", (
                "the service refused this caller's network location, not its "
                "credential. Check the resource's public network access and "
                "access-rule settings, and whether this deployment's outbound "
                "address is permitted -- a Container Apps environment with no "
                "dedicated subnet egresses from a shared address that cannot be "
                "allow-listed. Granting an RBAC role does not change this."
            )
        return "not_accessible", (
            "the credential was accepted but is not authorised for this "
            "deployment or index"
        )

    if status == 401:
        return "not_accessible", (
            "the credential was rejected -- wrong, rotated, or a secretref that "
            "resolved to an empty string, which looks identical to a working one "
            "in the portal"
        )

    if status == 404:
        return "not_found", (
            "no such deployment or index at this endpoint and api-version"
        )

    return "unavailable", f"the service returned {status}"

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
    last_status: int | None = None

    for attempt in range(max_attempts):
        try:
            response = await client.post(url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            last_status = None
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
                last_error = response.text[:BODY_BUDGET]
                last_status = response.status_code
                break
            await asyncio.sleep(delay)
            continue

        body = response.text[:BODY_BUDGET]
        category, hint = classify(response.status_code, body)
        raise AzureEndpointError(
            what, category=category, hint=hint,
            status=response.status_code, azure_message=body, endpoint=url,
        )

    if last_status is None:
        # No HTTP response at all. The transport error is the diagnosis here, so
        # it *is* the hint -- there is no service message to quarantine.
        raise AzureEndpointError(
            what, category="unreachable", hint=last_error,
            endpoint=url, attempts=max_attempts,
        )

    category, hint = classify(last_status, last_error)
    raise AzureEndpointError(
        what, category="unavailable", hint=hint, status=last_status,
        azure_message=last_error, endpoint=url, attempts=max_attempts,
    )


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
