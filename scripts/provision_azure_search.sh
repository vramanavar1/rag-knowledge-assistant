#!/usr/bin/env bash
#
# Provision the Azure resources this app needs, and print the environment
# variables to export.
#
#   bash scripts/provision_azure_search.sh
#
# Idempotent: re-running it will not recreate anything that already exists.
# The search *index* itself is not created here -- `scripts/ingest.py` creates
# it with a PUT, which is create-or-update, so the schema always matches the
# code that queries it.

set -euo pipefail

RESOURCE_GROUP="${RESOURCE_GROUP:-rg-rag-knowledge-assistant}"
LOCATION="${LOCATION:-eastus}"
SEARCH_NAME="${SEARCH_NAME:-srch-northwind-kb}"
# basic is the lowest tier that includes the semantic ranker. `free` works for
# vector + keyword hybrid but has no L2 reranker and a 50 MB storage cap, which
# is still ample for this 11-document corpus.
SEARCH_SKU="${SEARCH_SKU:-basic}"
OPENAI_NAME="${OPENAI_NAME:-}"
EMBEDDING_DEPLOYMENT="${EMBEDDING_DEPLOYMENT:-text-embedding-3-small}"
UTILITY_DEPLOYMENT="${UTILITY_DEPLOYMENT:-gpt-4o-mini}"

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }

say "Subscription"
az account show --query "{name:name, id:id}" -o tsv

say "Resource group: $RESOURCE_GROUP ($LOCATION)"
az group create --name "$RESOURCE_GROUP" --location "$LOCATION" -o none

say "Azure AI Search: $SEARCH_NAME (sku: $SEARCH_SKU)"
if az search service show --name "$SEARCH_NAME" \
      --resource-group "$RESOURCE_GROUP" -o none 2>/dev/null; then
  echo "  already exists"
else
  az search service create \
    --name "$SEARCH_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --location "$LOCATION" \
    --sku "$SEARCH_SKU" \
    --partition-count 1 \
    --replica-count 1 \
    -o none
  echo "  created"
fi

SEARCH_KEY="$(az search admin-key show \
  --service-name "$SEARCH_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --query primaryKey -o tsv)"

# ---------------------------------------------------------------------------
# Embedding deployment.
#
# Retrieval quality depends on this far more than on any tuning parameter: with
# no embedding deployment the app falls back to a local hashed-feature embedder
# that has no notion of synonymy, so "time off" will not match "PTO".
#
# Model versions are NOT hard-coded here.  `--model-version` is required by the
# CLI, and a pinned value goes stale: gpt-4o-mini 2024-07-18 was deprecated on
# 2026-03-31 and can no longer be deployed at all, so the old pinned pair failed
# with ServiceModelDeprecated on any resource that did not already have it.
# resolve_version() asks the resource what it will actually accept.
# ---------------------------------------------------------------------------

# Print the versions of $1 this resource offers, and echo the one to deploy:
# newest non-deprecated, preferring the version Azure marks as the default.
# Echoes nothing when there is no deployable version.
resolve_version() {
  local model="$1" rows
  rows="$(az cognitiveservices account list-models \
            --name "$OPENAI_NAME" --resource-group "$RESOURCE_GROUP" \
            --query "[?model.name=='${model}' && model.format=='OpenAI'].[model.version, model.lifecycleStatus, model.isDefaultVersion]" \
            -o tsv 2>/dev/null)" || true

  if [[ -z "$rows" ]]; then
    echo "  could not list versions of $model" >&2
    return 0
  fi

  echo "  versions of $model:" >&2
  printf '    %s\n' "$rows" >&2

  # Drop Deprecated/Legacy, then sort so isDefaultVersion=True wins, newest next.
  awk -F'\t' '$2 != "Deprecated" && $2 != "Legacy" {
                print ($3 == "True" ? 1 : 0) "\t" $1
              }' <<<"$rows" \
    | sort -r -k1,1 -k2,2 \
    | head -n1 \
    | cut -f2
}

if [[ -n "$OPENAI_NAME" ]]; then
  say "Azure OpenAI deployments on $OPENAI_NAME"

  for spec in "$EMBEDDING_DEPLOYMENT:text-embedding-3-small" \
              "$UTILITY_DEPLOYMENT:gpt-4o-mini"; do
    IFS=: read -r name model <<<"$spec"
    if az cognitiveservices account deployment show \
         --name "$OPENAI_NAME" --resource-group "$RESOURCE_GROUP" \
         --deployment-name "$name" -o none 2>/dev/null; then
      echo "  $name already exists"
      continue
    fi

    version="$(resolve_version "$model")"
    if [[ -z "$version" ]]; then
      echo "  no deployable version of $model -- deploy one in the portal, or" >&2
      echo "  pick a model from the list above, then re-run." >&2
      continue
    fi

    echo "  deploying $model $version as '$name'"
    az cognitiveservices account deployment create \
      --name "$OPENAI_NAME" \
      --resource-group "$RESOURCE_GROUP" \
      --deployment-name "$name" \
      --model-name "$model" \
      --model-version "$version" \
      --model-format OpenAI \
      --sku-capacity 30 \
      --sku-name Standard \
      -o none && echo "  $name created"
  done
else
  cat <<'NOTE'

  OPENAI_NAME not set, so no model deployments were created.
  Set it and re-run to create the embedding and utility deployments:

      OPENAI_NAME=<your-aoai-resource> bash scripts/provision_azure_search.sh
NOTE
fi

say "Export these, then run the ingester"
cat <<EOF
export RETRIEVER_BACKEND=azure
export AZURE_SEARCH_ENDPOINT=https://${SEARCH_NAME}.search.windows.net
export AZURE_SEARCH_API_KEY=${SEARCH_KEY}
export AZURE_SEARCH_INDEX=northwind-kb
export AZURE_SEARCH_SEMANTIC=$([[ "$SEARCH_SKU" == "free" ]] && echo false || echo true)

python scripts/ingest.py --force        # creates the index and loads it
uvicorn rag.api.app:app --app-dir src --port 8000
EOF

if [[ "$SEARCH_SKU" == "free" ]]; then
  cat <<'NOTE'

  Note: the free tier has no semantic ranker, so AZURE_SEARCH_SEMANTIC is off
  above and the pipeline will fall back to its own LLM reranker. The app also
  detects this at runtime and degrades once, loudly, rather than failing.
NOTE
fi
