"""Environment-driven configuration.

One ``Settings`` object, built once at import of ``get_settings()``.  Every
Azure-dependent value is optional: when a credential is missing the matching
provider degrades to a local implementation and logs which one is live, so a
demo can never silently answer from a fallback the operator did not expect.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:  # optional convenience; the app works fine without a .env file
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - dotenv is optional
    pass


REPO_ROOT = Path(__file__).resolve().parents[2]


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name) or default)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name) or default)
    except ValueError:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env(name).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


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

    origins = _env("API_ALLOWED_ORIGINS")
    if origins:
        s.allowed_origins = [o.strip() for o in origins.split(",") if o.strip()]

    return s


_settings: Settings | None = None


def get_settings(refresh: bool = False) -> Settings:
    global _settings
    if _settings is None or refresh:
        _settings = _load()
    return _settings
