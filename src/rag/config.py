"""Environment-driven configuration.

One ``Settings`` object, built once at import of ``get_settings()``.  Every
Azure-dependent value is optional: when a credential is missing the matching
provider degrades to a local implementation and logs which one is live, so a
demo can never silently answer from a fallback the operator did not expect.

**One exception, and only one.** With ``RETRIEVER_BACKEND=azure`` the index is a
remote artifact of a fixed vector width, so degrading the *embedder* can produce
a combination that cannot serve a correct answer at all. ``rag.startup`` checks
that at boot and refuses to start rather than failing on the first question.
Everything else, and every local path, degrades exactly as described above.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from rag.models import content_fingerprint

try:  # optional convenience; the app works fine without a .env file
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - dotenv is optional
    pass


REPO_ROOT = Path(__file__).resolve().parents[2]


# Every variable the helpers below are asked for, recorded as a side effect of
# reading it. A hand-kept list would drift from the reads the first time someone
# adds a setting; this cannot, because adding an `_env(...)` call *is* the
# registration. Reset at the top of `_load()` so `get_settings(refresh=True)`
# rebuilds rather than accumulating stale entries.
_ENV_READS: dict[str, dict[str, object]] = {}


def _record(name: str, default: object) -> None:
    raw = os.getenv(name)
    _ENV_READS[name] = {
        # `present` and the value are tracked separately: a variable set to the
        # empty string is a different diagnosis from one never set at all, and
        # a Key Vault secretref that fails to resolve produces exactly the
        # former while looking correct in the portal.
        "present": raw is not None,
        "raw": raw or "",
        "default": str(default).lower() if isinstance(default, bool) else str(default),
    }


def _env(name: str, default: str = "") -> str:
    _record(name, default)
    return (os.getenv(name) or default).strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name) or default)
    except ValueError:
        return default
    finally:
        # `_env` above recorded its own empty default; correct it to the typed
        # one so the report shows the value that actually applies when unset.
        _record(name, default)


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name) or default)
    except ValueError:
        return default
    finally:
        _record(name, default)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env(name).lower()
    _record(name, default)
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


# Matched against the variable *name*, so a secret is redacted even when its
# value looks innocuous. Verified against the current set: catches
# AZURE_OPENAI_API_KEY, AZURE_SEARCH_API_KEY and
# APPLICATIONINSIGHTS_CONNECTION_STRING, with no false positives among the rest.
_SECRET_RE = re.compile(
    r"KEY|SECRET|TOKEN|PASSWORD|CONNECTION_STRING|CREDENTIAL|SAS", re.IGNORECASE
)

# Below this length a truncated SHA-256 is worth guessing offline, so short
# secrets get a length and nothing else. An 8-hex prefix of a 32-char Azure key
# leaks nothing practical; the same prefix of a 6-char password does.
_FINGERPRINT_MIN_LEN = 16


def redacted_env() -> dict[str, str]:
    """Every variable `_load()` read, with secret values never rendered.

    Unset variables are included deliberately. A missing variable simply does
    not appear in a raw ``os.environ`` dump, and "never configured" -- or a
    secretref that resolved to nothing -- is the failure this report exists to
    catch.

    Redaction does not depend on ``SHOW_ENV_VALUES``: ``/health`` carries no auth
    dependency, so the flag gates whether the block appears at all, never
    whether a secret is visible.
    """
    report: dict[str, str] = {}

    for name in sorted(_ENV_READS):
        read = _ENV_READS[name]
        default = str(read["default"])

        if not read["present"]:
            report[name] = f"<UNSET> (default: {default})" if default else "<UNSET>"
            continue

        # Stripped to match what `_env` itself returns, so the report shows the
        # value the app is actually using -- whitespace-only reads as empty.
        value = str(read["raw"]).strip()

        if not value:
            report[name] = "<EMPTY>"
        elif _SECRET_RE.search(name):
            if len(value) >= _FINGERPRINT_MIN_LEN:
                report[name] = (f"<set len={len(value)} "
                                f"sha256:{content_fingerprint(value)[:8]}>")
            else:
                report[name] = f"<set len={len(value)}>"
        else:
            report[name] = value

    return report


@dataclass
class Settings:
    # ---- Azure OpenAI -----------------------------------------------------
    # Explicit opt-in. Credentials alone are not enough -- see has_azure_openai.
    aoai_enabled: bool = False
    aoai_endpoint: str = ""
    aoai_api_key: str = ""
    aoai_api_version: str = "2024-10-21"
    aoai_chat_deployment: str = ""
    aoai_utility_deployment: str = ""
    aoai_embedding_deployment: str = ""
    aoai_embedding_dimensions: int = 1536
    # Whether a width was *asked for*, as opposed to defaulted. Only an explicit
    # request is sent to Azure; otherwise the model returns its native width and
    # the probe can measure it honestly.
    aoai_embedding_dimensions_explicit: bool = False

    # ---- TLS (for networks that intercept HTTPS) --------------------------
    azure_ca_bundle: str = ""
    azure_use_system_certs: bool = False
    azure_tls_verify: bool = True
    # HTTP connection pool per provider, and the embedding fan-out width.
    http_max_connections: int = 200
    embed_concurrency: int = 8

    # ---- Azure AI Search --------------------------------------------------
    retriever_backend: str = "local"          # local | azure
    search_endpoint: str = ""
    search_api_key: str = ""
    search_index: str = "northwind-kb"
    search_api_version: str = "2024-07-01"
    search_semantic: bool = True

    # ---- Behaviour --------------------------------------------------------
    profile: str = "improved"                 # improved | baseline
    source_dir: Path = field(default_factory=lambda: REPO_ROOT / "KnwoledgeBaseDocuments")
    data_dir: Path = field(default_factory=lambda: REPO_ROOT / "data")

    retrieve_top_k: int = 20
    context_top_k: int = 5
    min_relevance: float = 4.0
    max_context_chars: int = 12000
    enable_answer_cache: bool = True

    # Baseline profile knobs (the deliberately naive "before" system).
    baseline_chunk_chars: int = 512
    baseline_top_k: int = 5

    # ---- Observability ----------------------------------------------------
    log_level: str = "INFO"
    log_format: str = "json"
    appinsights_connection_string: str = ""
    # Adds the redacted env report to /health. Off by default: it is a
    # troubleshooting aid, not something to leave on a public endpoint.
    show_env_values: bool = False
    # What to do when the startup contract is violated. `unready` keeps the
    # process alive but fails readiness, so the diagnosis is reachable over HTTP;
    # `crash` exits at boot, which is safer but leaves nothing to interrogate --
    # no replica means no /health and no log stream.
    startup_fail_mode: str = "unready"

    # ---- API --------------------------------------------------------------
    allowed_origins: list[str] = field(default_factory=lambda: ["http://localhost:8000"])

    # ---- Derived ----------------------------------------------------------
    @property
    def is_baseline(self) -> bool:
        return self.profile == "baseline"

    @property
    def azure_openai_credentials_present(self) -> bool:
        """Credentials exist, whether or not they are permitted to be used."""
        return bool(self.aoai_endpoint and self.aoai_api_key and self.aoai_chat_deployment)

    @property
    def has_azure_openai(self) -> bool:
        """Azure OpenAI is configured **and** explicitly switched on.

        The flag exists because credentials are frequently inherited rather
        than chosen: a machine-wide ``AZURE_OPENAI_ENDPOINT`` /
        ``AZURE_OPENAI_DEPLOYMENT_NAME`` would otherwise make a run described as
        "local" send document passages to a cloud model, and charge for it,
        without anyone opting in.

        The search backend already fails closed -- ``RETRIEVER_BACKEND=azure``
        without credentials falls back to local rather than erroring. This makes
        Azure OpenAI behave the same way, so "local" means local across
        retrieval, embeddings and generation alike.
        """
        return bool(self.aoai_enabled and self.azure_openai_credentials_present)

    @property
    def has_azure_search(self) -> bool:
        return bool(self.search_endpoint and self.search_api_key)

    @property
    def utility_deployment(self) -> str:
        return self.aoai_utility_deployment or self.aoai_chat_deployment

    def index_path(self) -> Path:
        """Index file is profile-scoped so baseline and improved never collide."""
        return self.data_dir / f"index.{self.profile}.json"

    def manifest_path(self) -> Path:
        return self.data_dir / f"manifest.{self.profile}.json"

    def embedding_cache_path(self) -> Path:
        # Cache is keyed by provider+dimension, not by profile: the same chunk
        # text embedded by the same model yields the same vector either way.
        return self.data_dir / "embedding_cache.json"


def _load() -> Settings:
    s = Settings()

    # Rebuilt from scratch each load, so `get_settings(refresh=True)` reports the
    # current environment rather than merging it with the previous one.
    _ENV_READS.clear()

    s.aoai_enabled = _env_bool("AZURE_OPENAI_ENABLED", False)
    s.aoai_endpoint = _env("AZURE_OPENAI_ENDPOINT").rstrip("/")
    s.aoai_api_key = _env("AZURE_OPENAI_API_KEY")
    s.aoai_api_version = _env("AZURE_OPENAI_API_VERSION", s.aoai_api_version)
    # AZURE_OPENAI_DEPLOYMENT_NAME is the variable this machine already exports.
    s.aoai_chat_deployment = _env("AZURE_OPENAI_CHAT_DEPLOYMENT") or _env(
        "AZURE_OPENAI_DEPLOYMENT_NAME"
    )
    s.aoai_utility_deployment = _env("AZURE_OPENAI_UTILITY_DEPLOYMENT")
    s.aoai_embedding_deployment = _env("AZURE_OPENAI_EMBEDDING_DEPLOYMENT")
    s.aoai_embedding_dimensions = _env_int("AZURE_OPENAI_EMBEDDING_DIMENSIONS", 1536)
    # Read straight from the environment, not via `_env`, which would re-record
    # the variable with an empty default and corrupt the SHOW_ENV_VALUES report.
    s.aoai_embedding_dimensions_explicit = bool(
        (os.getenv("AZURE_OPENAI_EMBEDDING_DIMENSIONS") or "").strip()
    )

    s.azure_ca_bundle = _env("AZURE_CA_BUNDLE")
    s.azure_use_system_certs = _env_bool("AZURE_USE_SYSTEM_CERTS", False)
    s.azure_tls_verify = _env_bool("AZURE_TLS_VERIFY", True)
    s.http_max_connections = _env_int("AZURE_HTTP_MAX_CONNECTIONS",
                                      s.http_max_connections)
    s.embed_concurrency = _env_int("AZURE_EMBED_CONCURRENCY",
                                   s.embed_concurrency)

    s.retriever_backend = _env("RETRIEVER_BACKEND", "local").lower()
    s.search_endpoint = _env("AZURE_SEARCH_ENDPOINT").rstrip("/")
    s.search_api_key = _env("AZURE_SEARCH_API_KEY")
    s.search_index = _env("AZURE_SEARCH_INDEX", s.search_index)
    s.search_api_version = _env("AZURE_SEARCH_API_VERSION", s.search_api_version)
    s.search_semantic = _env_bool("AZURE_SEARCH_SEMANTIC", True)

    s.profile = _env("RAG_PROFILE", "improved").lower()
    if src := _env("RAG_SOURCE_DIR"):
        p = Path(src)
        s.source_dir = p if p.is_absolute() else REPO_ROOT / p
    if dat := _env("RAG_DATA_DIR"):
        p = Path(dat)
        s.data_dir = p if p.is_absolute() else REPO_ROOT / p

    s.retrieve_top_k = _env_int("RAG_RETRIEVE_TOP_K", s.retrieve_top_k)
    s.context_top_k = _env_int("RAG_CONTEXT_TOP_K", s.context_top_k)
    s.min_relevance = _env_float("RAG_MIN_RELEVANCE", s.min_relevance)
    s.max_context_chars = _env_int("RAG_MAX_CONTEXT_CHARS", s.max_context_chars)
    s.enable_answer_cache = _env_bool("RAG_ENABLE_ANSWER_CACHE", True)

    s.log_level = _env("LOG_LEVEL", "INFO").upper()
    s.log_format = _env("LOG_FORMAT", "json").lower()
    s.appinsights_connection_string = _env("APPLICATIONINSIGHTS_CONNECTION_STRING")
    s.show_env_values = _env_bool("SHOW_ENV_VALUES", False)
    s.startup_fail_mode = _env("STARTUP_FAIL_MODE", "unready").lower()

    origins = _env("API_ALLOWED_ORIGINS")
    if origins:
        s.allowed_origins = [o.strip() for o in origins.split(",") if o.strip()]

    # Read here for the report only. `auth.py` reads API_ALLOW_ANONYMOUS itself,
    # per-request via os.getenv, and keeps doing so -- routing it through
    # Settings would move the read to load time and change when a change to it
    # takes effect. It is recorded because a report that omits the switch
    # governing anonymous access is a poor troubleshooting aid.
    _record("API_ALLOW_ANONYMOUS", True)

    return s


_settings: Settings | None = None


def get_settings(refresh: bool = False) -> Settings:
    global _settings
    if _settings is None or refresh:
        _settings = _load()
    return _settings
