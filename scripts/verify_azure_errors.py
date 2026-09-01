"""Prove every Azure endpoint failure is described in our words, not Azure's.

    python scripts/verify_azure_errors.py

`rag.providers.http.classify` is pure, so this runs with no network, no Azure
subscription and no spend.

The rule it enforces is narrow and worth stating plainly. Azure OpenAI answers a
data-plane access-rule rejection with:

    Access denied due to Virtual Network/Firewall rules.

It sends that sentence whether or not a virtual network exists -- it is the one
message `networkAcls` and `publicNetworkAccess` share. This deployment has no
VNet anywhere, and pasting that sentence into our own error text sent a real
investigation looking for one. So: the service's words are kept, in
`azure_message` alone; every string we author says what this process observed.

Exits non-zero on any failure, so it can gate CI.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from rag.providers.http import (  # noqa: E402
    AzureEndpointError,
    classify,
    headline,
)

_failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok    {label}")
    else:
        _failures.append(label)
        print(f"  FAIL  {label}{f' -- {detail}' if detail else ''}")


# The bodies these services really send.
ACCESS_RULE_403 = (
    '{"error":{"code":"403","message":"Access denied due to '
    'Virtual Network/Firewall rules."}}'
)
PLAIN_403 = (
    '{"error":{"code":"AuthorizationFailed","message":"The principal does not '
    'have permission to perform action on this resource."}}'
)
BAD_KEY_401 = (
    '{"error":{"code":"401","message":"Access denied due to invalid '
    'subscription key. Make sure to provide a valid key."}}'
)
NOT_FOUND_404 = (
    '{"error":{"code":"DeploymentNotFound","message":"The API deployment for '
    'this resource does not exist."}}'
)
SEARCH_403 = (
    '{"error":{"code":"","message":"Forbidden by the resource\'s network rules."}}'
)

# Words that belong to Azure and must never reach a string we author.
BANNED = ("vnet", "virtual network", "firewall")


def main() -> int:
    print("Categories")
    for label, status, body, expected in (
        ("access-rule 403 -> not_accessible", 403, ACCESS_RULE_403, "not_accessible"),
        ("permissions 403 -> not_accessible", 403, PLAIN_403, "not_accessible"),
        ("search 403     -> not_accessible", 403, SEARCH_403, "not_accessible"),
        ("401            -> not_accessible", 401, BAD_KEY_401, "not_accessible"),
        ("404            -> not_found", 404, NOT_FOUND_404, "not_found"),
        ("429            -> unavailable", 429, "rate limit exceeded", "unavailable"),
        ("503            -> unavailable", 503, "service unavailable", "unavailable"),
    ):
        category, _ = classify(status, body)
        check(label, category == expected, f"got {category}")

    print("\nThe two 403s are told apart")
    access_hint = classify(403, ACCESS_RULE_403)[1]
    perms_hint = classify(403, PLAIN_403)[1]
    check("different hints", access_hint != perms_hint)
    check("access-rule hint names the network location",
          "network location" in access_hint, access_hint)
    check("access-rule hint rules out RBAC as the fix",
          "RBAC" in access_hint, access_hint)
    check("permissions hint names authorisation",
          "authorised" in perms_hint, perms_hint)
    check("401 hint names an empty secretref",
          "secretref" in classify(401, BAD_KEY_401)[1])

    print("\nHeadlines are in our vocabulary")
    for category, status, expected in (
        ("not_accessible", 403, "endpoint not accessible (HTTP 403)"),
        ("not_accessible", 401, "endpoint not accessible (HTTP 401)"),
        ("not_found", 404, "endpoint or deployment not found (HTTP 404)"),
        ("unreachable", None, "endpoint unreachable"),
    ):
        line = headline("Azure OpenAI embeddings", category, status)
        check(f"{category}/{status}", line.endswith(expected), line)

    print("\nAzure's wording is quarantined")
    exc = AzureEndpointError(
        "Azure OpenAI embeddings",
        category="not_accessible",
        hint=access_hint,
        status=403,
        azure_message=ACCESS_RULE_403,
        endpoint="https://example.openai.azure.com/...",
    )
    rendered = str(exc).lower()
    for word in BANNED:
        check(f"str(exc) omits {word!r}", word not in rendered, str(exc))
    for status, body in ((403, ACCESS_RULE_403), (403, SEARCH_403),
                         (401, BAD_KEY_401), (404, NOT_FOUND_404)):
        hint = classify(status, body)[1].lower()
        for word in BANNED:
            check(f"hint for {status} omits {word!r}", word not in hint, hint)

    check("azure_message still carries the original",
          "Virtual Network/Firewall" in exc.azure_message)
    check("log_fields exposes it separately",
          exc.log_fields()["azure_message"] == ACCESS_RULE_403)
    check("log_fields carries the category",
          exc.log_fields()["category"] == "not_accessible")

    print("\nStill a RuntimeError, so existing handlers keep working")
    check("subclasses RuntimeError", isinstance(exc, RuntimeError))

    print("\nTransport failure keeps its own category")
    unreachable = AzureEndpointError(
        "Azure OpenAI embeddings",
        category="unreachable",
        hint="ConnectError: certificate verify failed",
        attempts=4,
    )
    check("category is unreachable", unreachable.category == "unreachable")
    check("status is None", unreachable.status is None)
    check("the transport error is the hint",
          "certificate verify failed" in unreachable.hint)

    if _failures:
        print(f"\nFAILED: {len(_failures)} check(s): {', '.join(_failures)}")
        return 1
    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
