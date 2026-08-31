"""An offline stand-in for the Azure AI Search REST API.

Used by ``scripts/verify_pipeline.py`` to exercise
:class:`rag.store.azure_search.AzureAISearchStore` for real -- issuing actual
HTTP requests, parsing actual responses -- without a network or an Azure
subscription.

**What this proves and what it does not.** It is a *contract* test: it checks
that the adapter speaks the API as documented -- correct index definition,
correct query shapes, correct OData filters, correct delete and merge
semantics. It cannot prove that my reading of the API matches Azure's actual
behaviour. A live smoke test against a real service is still required before
production, and `docs/pipeline.md` carries that checklist.

To keep the assertions meaningful the stub maintains a real in-memory index
rather than returning canned responses: documents are stored, filters are
evaluated, deletes remove rows and merges leave untouched fields alone. It also
records every request it receives, so a test can assert on request *shape*, not
only on the outcome.

Only the surface the adapter actually calls is implemented:

    PUT  /indexes/{name}                  create or update the index definition
    GET  /indexes/{name}                  read it back (the stored vector width)
    POST /indexes/{name}/docs/index       mergeOrUpload / merge / delete actions
    POST /indexes/{name}/docs/search      search, vectorQueries, filter, facets
    GET  /indexes/{name}/docs/$count      document count
"""

from __future__ import annotations

import json
import math
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import unquote, urlparse

# ---------------------------------------------------------------------------
# OData filter subset
# ---------------------------------------------------------------------------

_SEARCH_IN = re.compile(
    r"(not\s+)?search\.in\(\s*(\w+)\s*,\s*'((?:[^']|'')*)'\s*,\s*'([^']*)'\s*\)",
    re.I,
)
_EQ = re.compile(r"(\w+)\s+eq\s+(?:'((?:[^']|'')*)'|(true|false))", re.I)


def _unquote_odata(value: str) -> str:
    return value.replace("''", "'")


def evaluate_filter(expression: str | None, doc: dict[str, Any]) -> bool:
    """Evaluate the OData subset ``AzureAISearchStore._build_filter`` emits.

    Deliberately narrow: it supports exactly the clause forms the adapter
    produces, joined by ``and``. Anything else raises, so a filter the adapter
    starts emitting but this stub does not understand fails loudly instead of
    silently matching everything -- which would turn a broken security filter
    into a passing test.
    """
    if not expression:
        return True

    for clause in re.split(r"\s+and\s+", expression.strip(), flags=re.I):
        clause = clause.strip()
        if not clause:
            continue

        if match := _SEARCH_IN.fullmatch(clause):
            negated, field, values, separator = match.groups()
            allowed = {
                _unquote_odata(v).strip()
                for v in _unquote_odata(values).split(separator or ",")
            }
            present = str(doc.get(field, "")) in allowed
            if bool(negated) == present:
                return False
            continue

        if match := _EQ.fullmatch(clause):
            field, text_value, bool_value = match.groups()
            expected: Any = (_unquote_odata(text_value) if text_value is not None
                             else bool_value.lower() == "true")
            if doc.get(field) != expected:
                return False
            continue

        raise ValueError(f"stub cannot evaluate OData clause: {clause!r}")

    return True


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", (text or "").lower()))


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


class StubIndex:
    """Minimal in-memory index with the semantics the adapter relies on."""

    def __init__(self) -> None:
        self.definition: dict[str, Any] = {}
        self.docs: dict[str, dict[str, Any]] = {}

    def apply(self, actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        results = []
        for action in actions:
            kind = action.get("@search.action", "mergeOrUpload")
            key = action.get("chunk_id")
            payload = {k: v for k, v in action.items()
                       if not k.startswith("@search.")}
            if kind == "delete":
                self.docs.pop(key, None)
            elif kind == "merge":
                if key not in self.docs:
                    results.append({"key": key, "status": False,
                                    "errorMessage": "document not found"})
                    continue
                # merge must not disturb fields it does not carry -- notably
                # content_vector, which is the whole point of patching.
                self.docs[key].update(payload)
            else:
                self.docs[key] = payload
            results.append({"key": key, "status": True})
        return results

    def search(self, body: dict[str, Any]) -> dict[str, Any]:
        query = body.get("search") or "*"
        top = int(body.get("top") or 50)
        skip = int(body.get("skip") or 0)
        select = body.get("select")
        vector_query = (body.get("vectorQueries") or [{}])[0]
        vector = vector_query.get("vector") or []
        semantic = body.get("queryType") == "semantic"
        expression = body.get("filter")

        matched = [d for d in self.docs.values() if evaluate_filter(expression, d)]

        query_tokens = set() if query == "*" else _tokens(query)
        scored = []
        for doc in matched:
            keyword = 0.0
            if query_tokens:
                haystack = _tokens(
                    f"{doc.get('embed_text', '')} {doc.get('content', '')} "
                    f"{doc.get('title', '')} {doc.get('section_path', '')}"
                )
                keyword = len(query_tokens & haystack) / len(query_tokens)
            dense = _cosine(vector, doc.get("content_vector") or []) if vector else 0.0
            scored.append((keyword + dense, keyword, doc))

        scored.sort(key=lambda row: -row[0])
        page = scored[skip:skip + top]

        fields = [f.strip() for f in select.split(",")] if select else None
        values = []
        for score, keyword, doc in page:
            row = {k: v for k, v in doc.items() if k != "content_vector"}
            if fields is not None:
                row = {k: v for k, v in row.items() if k in fields}
            row["@search.score"] = round(score, 6)
            if semantic:
                # Azure's semantic reranker scores 0-4.
                row["@search.rerankerScore"] = round(min(4.0, keyword * 4.0), 4)
            values.append(row)

        response: dict[str, Any] = {"value": values}

        if facets := body.get("facets"):
            computed: dict[str, list[dict[str, Any]]] = {}
            for facet in facets:
                field = facet.split(",")[0].strip()
                counts: dict[str, int] = {}
                for doc in matched:
                    key = doc.get(field)
                    if key is not None:
                        counts[key] = counts.get(key, 0) + 1
                computed[field] = [
                    {"value": value, "count": count}
                    for value, count in sorted(counts.items())
                ]
            response["@search.facets"] = computed

        return response


class AzureSearchStub:
    """A thread-backed HTTP server implementing the subset above.

    Usage::

        with AzureSearchStub() as stub:
            os.environ["AZURE_SEARCH_ENDPOINT"] = stub.endpoint
            ...
    """

    def __init__(self, *, api_key: str = "stub-key",
                 reject_semantic: bool = False,
                 delay_s: float = 0.0) -> None:
        self.api_key = api_key
        self.reject_semantic = reject_semantic
        # Models the round-trip latency of the real service. Without it every
        # call returns in microseconds, which hides exactly the behaviour a
        # concurrency measurement is trying to observe.
        self.delay_s = delay_s
        self.indexes: dict[str, StubIndex] = {}
        self.requests: list[dict[str, Any]] = []
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> "AzureSearchStub":
        stub = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:  # silence
                pass

            def _body(self) -> dict[str, Any]:
                length = int(self.headers.get("Content-Length") or 0)
                if not length:
                    return {}
                return json.loads(self.rfile.read(length).decode("utf-8"))

            def _send(self, code: int, payload: Any) -> None:
                if payload is None:
                    data = b""
                elif isinstance(payload, (dict, list)):
                    data = json.dumps(payload).encode("utf-8")
                else:
                    data = str(payload).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                if data:
                    self.wfile.write(data)

            def _handle(self, method: str) -> None:
                if stub.delay_s:
                    time.sleep(stub.delay_s)
                parsed = urlparse(self.path)
                path = unquote(parsed.path)
                body = self._body()
                stub.requests.append({"method": method, "path": path,
                                      "query": parsed.query, "body": body,
                                      "headers": dict(self.headers)})

                if self.headers.get("api-key") != stub.api_key:
                    self._send(403, {"error": {"message": "invalid api-key"}})
                    return
                if "api-version=" not in parsed.query:
                    self._send(400, {"error": {"message": "api-version required"}})
                    return

                # /indexes/{name}
                if match := re.fullmatch(r"/indexes/([^/]+)", path):
                    name = match.group(1)
                    if method == "GET":
                        # Read back the definition, which is how the adapter
                        # learns the width the index was actually built with.
                        # A 404 here is meaningful, not incidental: the adapter
                        # treats it as "no index yet" and reports width 0.
                        index = stub.indexes.get(name)
                        if index is None:
                            self._send(404, {"error": {"message": "no such index"}})
                            return
                        self._send(200, index.definition)
                        return
                    if method != "PUT":
                        self._send(405, {"error": {"message": "method not allowed"}})
                        return
                    index = stub.indexes.setdefault(name, StubIndex())
                    index.definition = body
                    self._send(201, body)
                    return

                # /indexes/{name}/docs/index
                if match := re.fullmatch(r"/indexes/([^/]+)/docs/index", path):
                    index = stub.indexes.get(match.group(1))
                    if index is None:
                        self._send(404, {"error": {"message": "no such index"}})
                        return
                    self._send(200, {"value": index.apply(body.get("value", []))})
                    return

                # /indexes/{name}/docs/search
                if match := re.fullmatch(r"/indexes/([^/]+)/docs/search", path):
                    index = stub.indexes.get(match.group(1))
                    if index is None:
                        self._send(404, {"error": {"message": "no such index"}})
                        return
                    if stub.reject_semantic and body.get("queryType") == "semantic":
                        # What a Free-tier service returns for a semantic query.
                        self._send(400, {"error": {"message":
                                   "Semantic search is not enabled for this service."}})
                        return
                    try:
                        self._send(200, index.search(body))
                    except ValueError as exc:
                        self._send(400, {"error": {"message": str(exc)}})
                    return

                # /indexes/{name}/docs/$count
                if match := re.fullmatch(r"/indexes/([^/]+)/docs/\$count", path):
                    index = stub.indexes.get(match.group(1))
                    if index is None:
                        self._send(404, {"error": {"message": "no such index"}})
                        return
                    self._send(200, len(index.docs))
                    return

                self._send(404, {"error": {"message": f"unhandled path {path}"}})

            def do_GET(self) -> None:      # noqa: N802
                self._handle("GET")

            def do_PUT(self) -> None:      # noqa: N802
                self._handle("PUT")

            def do_POST(self) -> None:     # noqa: N802
                self._handle("POST")

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def __enter__(self) -> "AzureSearchStub":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    # -- inspection --------------------------------------------------------

    @property
    def endpoint(self) -> str:
        assert self._server is not None, "stub not started"
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def index(self, name: str) -> StubIndex:
        return self.indexes[name]

    def calls(self, path_suffix: str) -> list[dict[str, Any]]:
        return [r for r in self.requests if r["path"].endswith(path_suffix)]

    def last(self, path_suffix: str) -> dict[str, Any] | None:
        matches = self.calls(path_suffix)
        return matches[-1] if matches else None
