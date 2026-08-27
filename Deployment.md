# Deployment

How to run the Northwind Knowledge Assistant — on a laptop with nothing
installed, against real Azure services, and in production on Azure Container
Apps. Every command says what it does and why it is there.

> **Every command here is PowerShell**, and every `az` command is written to
> paste into a PowerShell terminal as-is. Verified on PowerShell 7.6; the only
> thing that needs 7.3+ is the `$PSNativeCommandUseErrorActionPreference`
> preamble in §5.2.
>
> On macOS or Linux three substitutions make any block work — the commands
> themselves are identical:
>
> | Here | bash / zsh |
> |---|---|
> | `` ` `` at end of line (continuation) | `\` |
> | `$env:NAME = "value"` | `export NAME=value` |
> | `$Name = "value"` (local) | `NAME=value` |
>
> `python …` and `docker …` invocations are the same on both. `curl.exe` is
> written in full deliberately: in Windows PowerShell 5.1 a bare `curl` is an
> alias for `Invoke-WebRequest`, which rejects `-s`, `-H` and `-d`. Naming the
> executable works in 5.1, in PowerShell 7 and in bash alike.

---

## Contents

1. [What you are deploying](#1-what-you-are-deploying)
2. [Prerequisites](#2-prerequisites)
3. [Path A — local, zero-install](#3-path-a--local-zero-install)
4. [Local testing](#4-local-testing)
5. [Path B — local app, real Azure services](#5-path-b--local-app-real-azure-services)
6. [Path C — production on Azure Container Apps](#6-path-c--production-on-azure-container-apps)
7. [Configuration reference](#7-configuration-reference)
8. [Operations](#8-operations)
9. [Troubleshooting](#9-troubleshooting)
10. [What is not included](#10-what-is-not-included)

---

## 1. What you are deploying

A FastAPI service that answers questions over an enterprise document corpus,
plus a single-page chat UI it serves itself. One process, five endpoints:

| Endpoint | Purpose |
|---|---|
| `GET /` | The chat UI |
| `GET /health` | Readiness probe — **503 when the index is empty** |
| `POST /api/v1/chat` | Ask a question |
| `GET /api/v1/documents` | What is indexed, trimmed to the caller |
| `POST /api/v1/ingest` | Re-sync the corpus (admin only) |
| `GET /docs` | OpenAPI |

### The one decision that shapes everything: which search backend

Retrieval sits behind a `SearchBackend` protocol with two implementations, and
the choice determines whether the service is stateless.

| | `RETRIEVER_BACKEND=local` (default) | `RETRIEVER_BACKEND=azure` |
|---|---|---|
| Index lives in | `data/index.{profile}.json` on local disk | Azure AI Search |
| Reranking | LLM reranker (~2 s, 1 extra model call) | The service's semantic ranker (in-request) |
| Stateless? | **No** — each replica holds its own index | **Yes** |
| Scale to >1 replica | Not meaningfully | Yes |
| Cost | Free | Search service (Basic ≈ $75/mo for the semantic ranker) |
| Good for | Local dev, CI, demos, evaluation | Production |

**Use the local backend for everything up to production, and the Azure backend
in production.** Nothing in the pipeline branches on which is active — see
[docs/pipeline.md](docs/pipeline.md).

### What is stateful, and where

Three pieces of state, and they behave differently:

- **The index** — external with the Azure backend, on disk with the local one.
- **The ingestion manifest** (`data/manifest.{profile}.json`) — **always local**,
  even in Azure mode. It records each document's content hash and the exact
  chunk ids it produced. Lose it and the next ingest re-embeds the whole corpus
  and, worse, **cannot detect deletions** — nothing remembers which documents
  used to exist. This is why ingestion must not run inside an ephemeral API
  container (§6.4).
- **The answer cache** — in-memory, per replica, scoped by department. Nothing
  is lost if it goes; hit rate just drops.

---

## 2. Prerequisites

### 2.1 Python

**Pinned to 3.14** — see [`.python-version`](.python-version), which `uv` and
`pyenv` read automatically. Verified here on **3.14.6** and **3.13.7**; the
package floor is 3.11 so a reviewer on an older interpreter is not blocked.

**Check which Python you are about to get before you create anything.** On
Windows these three can easily disagree, and that mismatch is the single most
common way to break this setup:

```powershell
py -0p              # every installed interpreter; * marks the launcher default
python --version    # what PATH resolves - NOT necessarily the same
```

### 2.2 Create a virtual environment

Pick the interpreter **explicitly**. Do not use a bare `python -m venv`: it
takes whatever `python` happens to resolve to, which may not be the pinned
version.

```powershell
# with uv (honours .python-version automatically)
uv venv
.\.venv\Scripts\Activate.ps1
uv pip install -r requirements.txt
```

```powershell
# without uv, naming the version explicitly
py -3.14 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

> **If `.venv` already exists, delete it before recreating it.**
> `python -m venv .venv` over an existing directory rewrites `pyvenv.cfg` and
> the `Scripts/` shims but **leaves `Lib/site-packages` untouched**. If the new
> interpreter differs from the one the packages were built for, everything
> imports fine until it reaches a compiled extension, and then you get
> `ModuleNotFoundError: No module named 'pydantic_core._pydantic_core'` —
> because wheels like `pydantic-core`, `lxml` and `PyMuPDF` ship a binary per
> Python version (`cp314-win_amd64.pyd` vs `cp313-...`).
>
> ```powershell
> Remove-Item -Recurse -Force .venv    # first
> ```

> Behind a TLS-inspecting corporate proxy, `uv pip install` fails with
> `invalid peer certificate: UnknownIssuer`. Add `--native-tls` to use the
> Windows trust store: `uv pip install --native-tls -r requirements.txt`.

> If PowerShell refuses to run the activation script, it is the execution
> policy, not the repo:
> `Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned`

### 2.3 Install the dependencies

```powershell
pip install -r requirements.txt
```

Seven packages: `fastapi`, `uvicorn`, `pydantic`, `httpx`, `python-dotenv`,
`PyMuPDF`, `python-docx`. There is no `azure-*` dependency — Azure OpenAI and
Azure AI Search are called over their REST APIs with `httpx`. All seven ship
prebuilt wheels, so no compiler is needed.

### 2.4 Optional — install the package for a shorter CLI

```powershell
pip install -e .
```

This is **not required**. Everything works without it. It buys you `rag ask
"..."` instead of `python scripts/cli.py ask "..."`, and makes `python -m
rag.cli` work — the package lives under `src/`, which `python -m` does not put
on `sys.path` on its own.

### 2.5 Check the setup before going further

```powershell
.\.venv\Scripts\python.exe -V     # expect 3.14.x
.\.venv\Scripts\python.exe -c "import fastapi, uvicorn, pydantic, httpx, dotenv, pymupdf, docx; print('dependencies OK')"
```

If that prints `dependencies OK`, §3 and §4 will work with no further setup.
Note it runs the venv's interpreter by full path rather than a bare `python` —
that is the check, not a formality: it confirms the interpreter *and* the
packages it loads are the pair you just installed.

### 2.6 Optional, for the Azure paths only (§5, §6)

- An Azure subscription and the `az` CLI, logged in (`az login`).
- Azure OpenAI with a chat deployment; ideally also an embedding deployment and
  a small utility deployment.
- Azure AI Search, Basic SKU or above if you want the semantic ranker.
- Docker, for §6.

---

## 3. Path A — local, zero-install

The fastest way to see it work. No Azure account, no credentials, no network.
Complete §2 first, then two commands:

```powershell
# 1. Build the index from the corpus in KnwoledgeBaseDocuments/
python scripts/ingest.py

# 2. Serve the API and UI
python -m uvicorn rag.api.app:app --app-dir src --port 8000
```

Open <http://localhost:8000>. Both commands are identical on PowerShell and
bash, and neither needs an environment variable.

### What step 1 does

Walks the corpus, parses each document preserving table structure, chunks it
section-aware with heading breadcrumbs, embeds each chunk, and writes
`data/index.improved.json` plus `data/manifest.improved.json`.

**On the first run**, when `data/` does not yet exist:

```
profile=improved  backend=local  embeddings=local-hashing
  11 new, 0 modified, 0 deleted, 0 unchanged
  chunks: +127 written, -0 purged, 127 total (22 tables)
  embeddings: 0 API batches, 0 cache hits
  superseded: Sales/Pricing2025.pdf
```

**On every run after that**, with the corpus unchanged:

```
profile=improved  backend=local  embeddings=local-hashing
  0 new, 0 modified, 0 deleted, 11 unchanged
  chunks: +0 written, -0 purged, 127 total (22 tables)
  embeddings: 0 API batches, 0 cache hits
```

> **`0 new … 11 unchanged` is success, not a failure.** Ingestion is
> incremental: each document is fingerprinted with a SHA-256 of its bytes, and
> a document whose content has not changed is neither re-parsed nor re-embedded.
> Re-running costs nothing. The line that tells you the index is fine is
> `127 total (22 tables)` — not the `new` count.
>
> If `data/` already existed when you first ran it (for example it shipped with
> your copy of the repo), your *first* run will show the second form. That is
> expected.

### Reading the output

| Line | Meaning |
|---|---|
| `11 new … 0 unchanged` | How many documents were classified as new, modified, deleted or unchanged this run |
| `+127 written, -0 purged` | Chunks added and removed **this run** |
| `127 total (22 tables)` | The size of the index now. **This is the number that matters.** `22 tables` confirms table-aware parsing worked |
| `0 API batches, 0 cache hits` | Embedding calls made. Zero here because no Azure embedding deployment is configured, so the local embedder ran |
| `superseded: Sales/Pricing2025.pdf` | Version reconciliation noticed the 2026 rate card replaces the 2025 one |

### Forcing a full rebuild

To reproduce the first-run output, or after changing the embedding model:

```powershell
python scripts/ingest.py --force
```

Or delete the index and start clean:

```powershell
Remove-Item -Recurse -Force data
```

### Confirming it worked

```powershell
python scripts/ingest.py --list
```

Expect 11 rows — department, type, effective date, version, current flag and
chunk count per document, with `Sales/Pricing2025.pdf` showing `NO` under
`cur` because the 2026 card superseded it.

Once the API is running, `Invoke-RestMethod http://localhost:8000/health` should
report `"documents": 11, "chunks": 127`. A **503** there means the index is
empty — run step 1.

> **"Local" here really is local.** Retrieval, embeddings and generation all
> run on this machine. Azure OpenAI is only used when you set
> `AZURE_OPENAI_ENABLED=true` (§5.1) — having credentials in your environment is
> deliberately *not* enough. If you do have them, startup says so and tells you
> they are being ignored:
>
> ```
> Azure OpenAI credentials are present but AZURE_OPENAI_ENABLED is not set,
> so they will NOT be used.  hint=set AZURE_OPENAI_ENABLED=true to use them
> ```

**What you get with no credentials.** The app runs completely, and says exactly
what it is running on:

```
no embedding deployment configured, using local embedder
embedding provider active  provider=local-hashing dimensions=768
no Azure OpenAI chat deployment configured; answers will be extractive
```

Answers are extractive rather than written by a model, and retrieval uses a
hashed-feature embedder with no notion of synonymy. Both are *reported*, never
silently substituted. It still answers correctly — "What is the minimum
password length?" returns the 12-character row from the policy table in ~12 ms.

Add `AZURE_OPENAI_ENDPOINT` / `_API_KEY` / `_CHAT_DEPLOYMENT` (§5) and the same
question comes back as a written, cited, verified answer.

### From the terminal instead

```powershell
python scripts/cli.py ask "What is the nightly hotel cap in London?"
python scripts/cli.py chat --department Sales          # multi-turn, department-scoped
python scripts/cli.py compare "What was the Professional tier price in 2025?"
```

`compare` runs the question through both the naive baseline and the improved
pipeline side by side — the quickest way to see a failure scenario and its fix.

> **Why `scripts/cli.py` and not `python -m rag.cli`?** The package lives under
> `src/`, and `python -m` puts the *current directory* on `sys.path`, never
> `src/` — so `python -m rag.cli` fails with `ModuleNotFoundError: No module
> named 'rag'` from a clean checkout. `scripts/cli.py` puts `src` on the path
> and hands over, exactly as `scripts/ingest.py` does. No environment variable,
> same command on every shell.
>
> If you ran `pip install -e .` (§2.4), then `rag ask "..."` and
> `python -m rag.cli ask "..."` both work as well.

---

## 4. Local testing

Local testing is fully supported and needs no Azure account. Four suites, in
increasing cost.

### 4.1 Pipeline coverage — 53 checks, no network

```powershell
python scripts/verify_pipeline.py
```

Walks all nine stages of the assignment's pipeline against a throwaway copy of
the corpus and asserts each. Expect `9/9 stages verified, 52 checks, 0
failure(s)` — 53 when `AZURE_OPENAI_ENABLED=true`, because stage 8 then
verifies a real completion instead of the extractive fallback. Exits non-zero on failure, so it can gate CI.

**Stage 5 (Azure AI Search) is covered here with no Azure account.** It runs
against an offline stub of the Search REST API
([`scripts/_azure_search_stub.py`](scripts/_azure_search_stub.py)), proving the
index definition, hybrid + semantic query construction, OData security filters,
delete-by-document, and metadata merge without re-embedding.

To run the *whole* pipeline on the Azure adapter rather than just stage 5:

```powershell
python scripts/verify_pipeline.py --backend azure
```

Expect `rerank_method=azure-semantic` and 2 model calls instead of 3 — the
service's reranker replaces the LLM one.

### 4.2 Document lifecycle — 23 assertions

```powershell
python scripts/verify_lifecycle.py
```

Copies the corpus to a temp directory, then adds, modifies and deletes
documents and asserts the index tracks it: no orphan chunks after an edit, zero
embedding calls when nothing changed, and supersession that reverses when a
newer document is removed. Expect `All lifecycle checks passed.`

### 4.3 Evaluation — 35 questions, baseline vs improved

Costs Azure OpenAI tokens (~$0.35 for both profiles) and needs a chat
deployment. Skip with `--no-judge` to halve the calls.

```powershell
python scripts/ingest.py --profile baseline      # build the naive "before" index
python scripts/ingest.py --profile improved

python eval/run_eval.py --profile baseline --out eval/results/baseline.json
python eval/run_eval.py --profile improved --out eval/results/improved.json
python eval/run_eval.py --compare eval/results/baseline.json `
                                  eval/results/improved.json `
                                  --report eval/results/comparison.md
```

Expect ~63% → ~97% answer correctness and 11% → 3% hallucination. A single
category runs in about a minute:

```powershell
python eval/run_eval.py --profile improved --category versioning --no-judge
```

To re-apply a scoring change to saved runs without spending tokens again:

```powershell
python eval/run_eval.py --rescore eval/results/baseline.json
```

### 4.4 Manual smoke — API and access control

```powershell
Invoke-RestMethod http://localhost:8000/health | ConvertTo-Json -Depth 6

# a Sales caller asking an HR question gets nothing
curl.exe -s localhost:8000/api/v1/chat `
  -H "Authorization: Bearer demo-sales" -H "Content-Type: application/json" `
  -d '{"question":"How many weeks of paid parental leave do I get?"}'

# the same question as HR
curl.exe -s localhost:8000/api/v1/chat `
  -H "Authorization: Bearer demo-hr" -H "Content-Type: application/json" `
  -d '{"question":"How many weeks of paid parental leave do I get?"}'
```

**Keep the body in single quotes.** PowerShell passes a single-quoted string to a
native command verbatim, so the JSON arrives intact. Double quotes would need
every inner `"` escaped, and `'{\"question\":…}'` — the shape that looks right —
sends literal backslashes to the API.

Demo tokens: `demo-admin`, `demo-finance`, `demo-hr`, `demo-it`, `demo-legal`,
`demo-sales`. `GET /api/v1/principals` lists them.

The first call should decline; the second should answer "12 weeks" citing
`HR/LeavePolicy.pdf`. If the first one answers, the department pre-filter is
not working — that is a security regression, not a quality one.

### 4.5 Expected failures

One evaluation case, `noans-03`, fails intermittently. It asks about a
"Standard plan" that does not exist, and the 2026 rate card happens to contain
the sentence *"Standard contract term is 12 months…"* — so every grounding check
legitimately passes on the wrong entity. It is documented in
[docs/evaluation.md](docs/evaluation.md#the-one-case-that-still-fails). A run
scoring 34/35 is expected; 33/35 or lower is a regression.

---

## 5. Path B — local app, real Azure services

### 5.1 Azure OpenAI

Easiest and shell-independent — copy `.env.example` to `.env` and edit it;
`python-dotenv` loads it automatically on every platform:

```powershell
Copy-Item .env.example .env
```

Or set them in the shell directly:

**Set `AZURE_OPENAI_ENABLED=true` as well as the credentials.** Nothing reaches Azure OpenAI without it — that is the point: it makes cloud use an explicit choice rather than a side effect of whatever is in your environment.

```powershell
$env:AZURE_OPENAI_ENABLED              = "true"
$env:AZURE_OPENAI_ENDPOINT             = "https://<resource>.openai.azure.com"
$env:AZURE_OPENAI_API_KEY              = "<key>"
$env:AZURE_OPENAI_CHAT_DEPLOYMENT      = "gpt-4o"
$env:AZURE_OPENAI_UTILITY_DEPLOYMENT   = "gpt-4o-mini"
$env:AZURE_OPENAI_EMBEDDING_DEPLOYMENT = "text-embedding-3-small"
```

These set the variables for **this terminal session only**. `.env` is the durable
option, and the one to prefer if you will come back to this tomorrow.

Three things worth knowing:

- **The utility deployment matters more than it looks.** Reranking, query
  condensation and answer verification are ~75% of the model calls and ~78% of
  the latency. Pointing them at a mini deployment is the single largest cost and
  speed win available, and costs nothing in quality — they are scoring and
  checking tasks, not synthesis. If the deployment does not exist the app falls
  back to the chat deployment.
- **The embedding deployment is the largest quality lever.** Without it,
  retrieval runs on a hashed-feature embedder with no notion of synonymy.
- **Changing the embedding model invalidates the index.** Vector widths must
  match; the store logs `embedding dimension changed … will be dropped`. Re-run
  `python scripts/ingest.py --force`.

Verify what is live:

```powershell
python scripts/cli.py ask "What is the minimum password length?"
# startup log should read: chat provider active  provider=azure-openai:<deployment>
```

### 5.2 Azure AI Search

Creates the resource group, the search service, and the embedding and utility
deployments on the Azure OpenAI resource named by `$OpenAiName` — clear that to
`""` to skip the deployments. Every step is idempotent: re-running changes
nothing that already exists.

**This is one script. Copy the whole block.** The two functions must be defined
before the loop that calls them, so running only part of it fails with
`The term 'Test-AzExists' is not recognized`.

Two things in it are worth understanding before you run it:

> **`if (az … -o none)` does not do what it looks like it does**, and this is the
> one thing to carry away from this section. Bash's `if cmd; then` tests the
> command's **exit code**; PowerShell's `if (cmd)` tests its **output**. With
> `-o none` there is no output *by definition*, so the condition is always
> `$false`, the `else` branch always runs, and a resource that already exists is
> created again. A direct translation of the bash idiom inverts its meaning.
>
> The probe must also suppress **all** streams, not just stderr.
> `az … 2>$null` leaves anything on stdout in the function's output, which is then
> returned *alongside* the boolean — and a two-element array is truthy, so every
> probe answers "already exists" and nothing is ever created. That is the same bug
> in a mirror. `*>$null` is what makes the return value the boolean and nothing
> else.
>
> And under `$PSNativeCommandUseErrorActionPreference = $true`, a probe for
> something that does **not** exist *throws* (`ProgramExitedWithNonZeroCode`)
> before you can read `$LASTEXITCODE` — the preamble that makes real failures
> fatal also makes asking a question fatal. `Test-AzExists` shadows that
> preference inside its own body, so the override lasts exactly as long as the
> probe.

**Model versions are not stable, so nothing here is hard-coded.**
`--model-version` is a *required* argument to `az cognitiveservices account
deployment create`, and a pinned value goes stale: `gpt-4o-mini` version
`2024-07-18` has been deprecated since 2026-03-31 and can no longer be deployed
at all. `Resolve-ModelVersion` asks the resource what it will actually accept,
prints the candidates, and picks one. The embedding deployment is worth the
trouble — **it is the largest quality lever in the system**; without it retrieval
falls back to a local hashed-feature embedder with no notion of synonymy, so
*"time off"* will not match *"PTO"*.

```powershell
$ErrorActionPreference = "Stop"   # for cmdlets; az is handled explicitly below

$ResourceGroup       = "rg-openai"
$Location            = "westus"
$SearchName          = "srch-northwind-kb"
$SearchSku           = "basic"       # lowest tier with the semantic ranker
$OpenAiName          = "oai-vsquarecloud"   # "" to skip the model deployments
$EmbeddingDeployment = "text-embedding-3-small"
$UtilityDeployment   = "gpt-4o-mini"
$EmbeddingVersion    = "auto"        # "auto" = pick a deployable version; or pin one
$UtilityVersion      = "auto"

# Every az call goes through one of these two, so the block behaves the same on
# Windows PowerShell 5.1 and PowerShell 7 -- their native-error defaults are
# opposite (see the note below). Both helpers neutralise BOTH preferences, and
# both assignments are function-scoped, so they restore on return and the block
# works whatever your profile has set.

# Both take their arguments through the automatic $args of a *simple* function,
# deliberately: a param() block with [Parameter()] makes a function "advanced",
# which adds the common parameters -- and then `-o tsv` fails to bind with
# "the parameter name 'o' is ambiguous. Possible matches include -OutVariable,
# -OutBuffer". A simple function passes -o straight through to az.

# Ask whether a resource exists. Never fatal -- a "not found" is an answer.
# It tests the EXIT CODE, not the output, and suppresses every stream so nothing
# can leak into the return value alongside the boolean.
function Test-AzExists {
    $ErrorActionPreference = "Continue"
    $PSNativeCommandUseErrorActionPreference = $false
    az @args -o none *>$null
    return $LASTEXITCODE -eq 0
}

# Make a change that must succeed. Stops the script, naming the command, if it
# does not. az's own stderr still reaches the console, so you see Azure's message
# as well as this one.
function Invoke-Az {
    $ErrorActionPreference = "Continue"
    $PSNativeCommandUseErrorActionPreference = $false
    az @args
    if ($LASTEXITCODE -ne 0) { throw "az $($args -join ' ') failed (exit $LASTEXITCODE)" }
}

# Print every version of $ModelName this resource offers, and return the one to
# deploy: newest non-deprecated, preferring the version Azure marks as default.
# Returns $null when nothing is deployable.
function Resolve-ModelVersion {
    param([string]$Account, [string]$ResourceGroup, [string]$ModelName, [string]$Pinned = "auto")

    $ErrorActionPreference = "Continue"
    $PSNativeCommandUseErrorActionPreference = $false
    $json = az cognitiveservices account list-models `
        --name $Account --resource-group $ResourceGroup -o json 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $json) {
        Write-Warning "  could not list models; falling back to '$Pinned'"
        return $(if ($Pinned -eq "auto") { $null } else { $Pinned })
    }

    $rows = ($json | ConvertFrom-Json) |
        Where-Object { $_.model.name -eq $ModelName -and $_.model.format -eq "OpenAI" }
    if (-not $rows) {
        Write-Host "  $ModelName is not offered by this resource"
        return $null
    }

    Write-Host "  versions of ${ModelName}:"
    foreach ($r in $rows | Sort-Object { $_.model.version } -Descending) {
        $m    = $r.model
        $mark = if ($m.isDefaultVersion) { " (default)" } else { "" }
        # Get-Date, not .Substring(): PS 7 parses the ISO date into [DateTime]
        # while Windows PowerShell 5.1 leaves it a string. This handles both.
        $dep  = if ($m.deprecation.inference) {
                    " retires $(Get-Date $m.deprecation.inference -Format 'yyyy-MM-dd')"
                } else { "" }
        Write-Host ("    {0,-12} {1}{2}{3}" -f $m.version, $m.lifecycleStatus, $mark, $dep)
    }

    if ($Pinned -ne "auto") { return $Pinned }

    $usable = $rows | Where-Object { $_.model.lifecycleStatus -notin @("Deprecated", "Legacy") }
    if (-not $usable) { return $null }

    # Sort on default-ness first, then version: a newer *Preview* build should not
    # win over the generally-available version Azure itself marks as the default.
    ($usable | Sort-Object @{ e = { [int][bool]$_.model.isDefaultVersion } },
                           @{ e = { $_.model.version } } -Descending |
        Select-Object -First 1).model.version
}

Invoke-Az account show --query "{name:name, id:id}" -o tsv
Invoke-Az group create --name $ResourceGroup --location $Location -o none

if (Test-AzExists search service show --name $SearchName --resource-group $ResourceGroup) {
    Write-Host "  search service already exists"
} else {
    Invoke-Az search service create `
        --name $SearchName --resource-group $ResourceGroup --location $Location `
        --sku $SearchSku --partition-count 1 --replica-count 1 -o none
    Write-Host "  search service created"
}

if ($OpenAiName) {
    @(
        @{ Name = $EmbeddingDeployment; Model = "text-embedding-3-small"; Pinned = $EmbeddingVersion }
        @{ Name = $UtilityDeployment;   Model = "gpt-4o-mini";            Pinned = $UtilityVersion }
    ) | ForEach-Object {
        if (Test-AzExists cognitiveservices account deployment show `
                --name $OpenAiName --resource-group $ResourceGroup --deployment-name $_.Name) {
            Write-Host "  $($_.Name) already exists, leaving it alone"
            return                                  # `return` in ForEach-Object = `continue`
        }

        $version = Resolve-ModelVersion -Account $OpenAiName -ResourceGroup $ResourceGroup `
                                        -ModelName $_.Model -Pinned $_.Pinned
        if (-not $version) {
            Write-Warning ("  no deployable version of $($_.Model). Deploy one in the portal, " +
                           "or set the deployment name to a model listed above and re-run.")
            return
        }

        Write-Host "  deploying $($_.Model) $version as '$($_.Name)'"
        Invoke-Az cognitiveservices account deployment create `
            --name $OpenAiName --resource-group $ResourceGroup `
            --deployment-name $_.Name `
            --model-name $_.Model --model-version $version --model-format OpenAI `
            --sku-capacity 30 --sku-name Standard -o none
        Write-Host "  $($_.Name) created"
    }
}
```

A typical run against a resource where the utility model was deployed by hand:

```
  text-embedding-3-small already exists, leaving it alone
  gpt-4o-mini already exists, leaving it alone
```

and on a resource where it is missing:

```
  versions of gpt-4o-mini:
    2026-08-01   Preview
    2026-04-14   GenerallyAvailable (default)
    2024-07-18   Deprecated retires 2026-03-31
  deploying gpt-4o-mini 2026-04-14 as 'gpt-4o-mini'
```

**Why every `az` call goes through a helper.** `az` is `az.cmd`, a batch shim, and
the two PowerShell editions treat a failing native command in **opposite** ways —
so no single set of preferences gets it right on both. Measured on each:

| With `$ErrorActionPreference = "Stop"` | Windows PowerShell 5.1 | PowerShell 7 |
|---|---|---|
| `az` exits non-zero and writes to stderr | **Terminating `NativeCommandError`** | Continues |
| Does `2>$null` / `*>$null` prevent that? | **No** | n/a |
| What actually makes a non-zero exit fatal | nothing — it is the *stderr write* that is fatal, warnings included | `$PSNativeCommandUseErrorActionPreference = $true` (7.3+) |

Read the 5.1 column carefully: there, `Stop` makes a resource that simply does not
exist yet **kill the script**, because `az` reports "not found" on stderr — and
redirecting the stream does not help. The obvious defensive move, `*>$null`, does
not work.

`Test-AzExists` and `Invoke-Az` sidestep the whole thing by setting both
preferences to their permissive values inside their own bodies and deciding on
`$LASTEXITCODE`, which means the same on every edition. A probe answers yes or no;
a change that fails stops the script by name. Nothing outside the helpers depends
on which shell you are in.

Then point the app at it and load the index. This runs **in the same session** as
the block above, which is where `$SearchName` and `$ResourceGroup` come from — the
first two lines fail loudly rather than quietly building
`https://.search.windows.net` if you opened a new terminal in between:

```powershell
if (-not $SearchName)    { throw "run the provisioning block above first (`$SearchName is not set)" }
if (-not $ResourceGroup) { throw "run the provisioning block above first (`$ResourceGroup is not set)" }

$env:RETRIEVER_BACKEND     = "azure"
$env:AZURE_SEARCH_ENDPOINT = "https://$SearchName.search.windows.net"
$env:AZURE_SEARCH_INDEX    = "northwind-kb"
$env:AZURE_SEARCH_API_KEY  = az search admin-key show `
    --service-name $SearchName --resource-group $ResourceGroup `
    --query primaryKey -o tsv
if (-not $env:AZURE_SEARCH_API_KEY) { throw "could not read the search admin key" }
if ($SearchSku -eq "free") { $env:AZURE_SEARCH_SEMANTIC = "false" }

python scripts/ingest.py --force        # creates the index and loads it
```

`az … -o tsv` writes to stdout, so PowerShell captures it directly — no `$( )`
needed, unlike bash.

> The equivalent bash script,
> [`scripts/provision_azure_search.sh`](scripts/provision_azure_search.sh), is
> still in the repo for macOS and Linux and takes the same settings as
> environment variables: `RESOURCE_GROUP=rg-rag SEARCH_SKU=basic
> OPENAI_NAME=<aoai> bash scripts/provision_azure_search.sh`.

The index is created by `ingest.py` with a `PUT`, which is create-or-update, so
the schema always matches the code that queries it — there is no separate
migration step.

`$SearchSku = "free"` works but has no semantic ranker; set
`AZURE_SEARCH_SEMANTIC=false`, or leave it and let the app degrade once, loudly,
on the first query.

**This path is contract-verified, not integration-verified.** The adapter has
been exercised against an offline stub of the REST API, not against a live
service. Run this checklist the first time you point it at real Azure:

```powershell
python scripts/ingest.py --force        # expect: index created, 127 chunks
python scripts/cli.py ask "What is the nightly hotel cap in London?"
#   expect: $350, and rerank_method=azure-semantic in the trace
python scripts/cli.py --department Sales ask "How many weeks of parental leave do I get?"
#   expect: declined
python eval/run_eval.py --profile improved --out eval/results/improved-azure.json
#   expect: at or above the local backend's scores
```

### 5.3 Behind a TLS-inspecting corporate proxy

If every Azure call fails with `CERTIFICATE_VERIFY_FAILED: unable to get local
issuer certificate`, your network is intercepting TLS with a CA that Python's
bundled certificate store does not know. Three options, best first:

```powershell
$env:AZURE_CA_BUNDLE        = "C:\path\to\corporate-root.pem"   # correct fix
$env:AZURE_USE_SYSTEM_CERTS = "true"    # use the Windows trust store
$env:AZURE_TLS_VERIFY       = "false"   # dev only; warns on every start
```

`AZURE_TLS_VERIFY=false` disables the check that the endpoint you are sending an
API key to is really Azure. Local development only — never in a deployed
environment.

**The `az` CLI does not read any of those.** It is a Python application using
`requests`, with its own trust configuration, so the same proxy that breaks the
app also breaks provisioning — `az cognitiveservices account list` fails with the
identical `CERTIFICATE_VERIFY_FAILED` while `az account show` still works, because
that one only reads the local token cache:

```powershell
$env:REQUESTS_CA_BUNDLE = "C:\path\to\corporate-root.pem"   # correct fix
$env:AZURE_CLI_DISABLE_CONNECTION_VERIFICATION = "1"        # dev only
```

Set both families if you are behind an intercepting proxy: `AZURE_CA_BUNDLE` for
the application, `REQUESTS_CA_BUNDLE` for `az`. They point at the same PEM file
and neither one covers the other.

---

## 6. Path C — production on Azure Container Apps

> **What in this section has been run.** The image build and everything you can
> do with the resulting container — §6.1 — were executed and verified. The
> `az containerapp` commands from §6.2 onward were **not**: they require an Azure
> subscription this was not deployed to. They follow the documented CLI surface.
> See [§10](#10-what-is-not-included).

### How the pieces connect

Worth reading before running anything, because two of these are easy to look for
in the wrong place.

**Nothing in the image connects to Azure AI Search.** The image ships with
`RETRIEVER_BACKEND=local`, so a production image starts on the *local* backend
with no index and correctly answers **503**. Three separate places set that
variable, and they do different jobs:

| Where | Value | What it governs |
|---|---|---|
| [`Dockerfile`](Dockerfile) `ENV` | `local` | The image default — deliberately inert, so an image cannot reach a real service by accident |
| §6.3, in **your shell** | `azure` | Where **ingestion writes**. This is what populates the index |
| §6.5, `az containerapp create --env-vars` | `azure` | Where the **running app reads**. This is the line that actually connects the backend |

**Your documents are already in the image.** `KnwoledgeBaseDocuments/` is copied
into every image, both variants — `BAKE_INDEX` controls whether the *index* is
pre-built, not whether the *documents* are present. §6.3 covers what that means
and what the alternatives are.

**§6 calls `az` directly**, unlike §5.2, whose `Test-AzExists` / `Invoke-Az`
helpers exist only within that block. Each step here is meant to be read and run
one at a time. If you want the same fail-fast behaviour, paste §5.2's two helper
functions into the session first and prefix the mutating calls with `Invoke-Az` —
but do not assume they are already defined.

### 6.1 Build the image

**Path C builds one image — the `prod` variant.** Build it explicitly rather
than relying on the `BAKE_INDEX` default:

```powershell
# Path C's image: state lives in Azure AI Search, so skip the index bake
docker build -t rag-assistant:prod --build-arg BAKE_INDEX=false .
```

The other variant belongs to §3 and §4, and is **not used by Path C**. It is
listed here only so the difference is visible:

```powershell
# demo image (§3/§4): index baked in at build time, runs with no Azure account
docker build -t rag-assistant:demo --build-arg BAKE_INDEX=true .
```

**Give them different tags.** Both commands previously wrote
`rag-assistant:latest`, so the second silently replaced the first — and since
§6.2 pushes by tag, you could ship the demo image to production without noticing.
It would start, serve a stale index baked at build time, and never contact Azure
AI Search.

**What `BAKE_INDEX` does and does not control.** `data/` is gitignored, so a clean
checkout has no index; `BAKE_INDEX=true` runs `scripts/ingest.py` during the build
so the demo image is self-contained and starts instantly. For the Azure-backed
image that index would be dead weight, built with whatever embedding provider
happened to be configured at build time — hence `false`.

It does **not** control whether your documents are in the image. They always are:
the Dockerfile copies `KnwoledgeBaseDocuments/` into the runtime stage either way.
See §6.3.

Test it locally before pushing:

```powershell
docker run --rm -p 8000:8000 rag-assistant:demo
curl.exe -s localhost:8000/health
```

Verified behaviour of the baked image on the 3.14 base — 342 MB, and:

```
container python     -> 3.14.7                       # matches .python-version
/health              -> 200  {"status":"ready","documents":11,"chunks":127}
docker exec … id     -> uid=10001(appuser)           # non-root
POST /api/v1/chat    -> answers, extractive with no credentials passed
POST without a token -> 401                          # API_ALLOW_ANONYMOUS=false
```

The `rag-assistant:prod` variant builds and returns **503** — the correct
readiness answer for a container with no index, not a defect. It keeps returning
503 until something sets `RETRIEVER_BACKEND=azure` and points it at a populated
index, which is [§6.5](#65-deploy)'s `--env-vars` line. Locally you can do the
same with `-e RETRIEVER_BACKEND=azure -e AZURE_SEARCH_ENDPOINT=… -e AZURE_SEARCH_API_KEY=…`.

Pass credentials at run time and the same image upgrades itself to written,
cited answers:

```powershell
docker run --rm -p 8000:8000 `
  -e AZURE_OPENAI_ENABLED=true `
  -e "AZURE_OPENAI_ENDPOINT=$env:AZURE_OPENAI_ENDPOINT" `
  -e "AZURE_OPENAI_API_KEY=$env:AZURE_OPENAI_API_KEY" `
  -e AZURE_OPENAI_CHAT_DEPLOYMENT=gpt-4o `
  rag-assistant:demo
# /health now reports llm_available: true; answers cite sources and are verified
```

Note the quoting: `-e "NAME=$env:VALUE"` wraps the **whole** `NAME=value` pair,
because `docker` expects it as a single argument. `-e NAME="$env:VALUE"` happens
to work too, but the first form is the one that survives a value containing a
space.

Worth knowing if your host sits behind a TLS-inspecting proxy: the container
does **not** inherit it. Calls that need `AZURE_TLS_VERIFY=false` on the host
(§5.3) succeeded from inside the Linux container with verification fully on.

### 6.2 Provision

```powershell
$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $true

$ResourceGroup = "rg-rag-prod"
$Location      = "eastus"

az group create -n $ResourceGroup -l $Location

# Search + model deployments: run the §5.2 block with $ResourceGroup set to the
# value above, and $OpenAiName set to your Azure OpenAI resource.

# Registry, Key Vault, observability, Container Apps environment
az acr create -g $ResourceGroup -n acrragprod --sku Basic --admin-enabled false
az keyvault create -g $ResourceGroup -n kv-rag-prod
az monitor app-insights component create `
  -g $ResourceGroup -a appi-rag-prod -l $Location --workspace "<log-analytics-id>"
az containerapp env create -g $ResourceGroup -n cae-rag-prod -l $Location
```

Push the image:

```powershell
az acr login -n acrragprod
docker tag rag-assistant:prod acrragprod.azurecr.io/rag-assistant:1.0.0
docker push acrragprod.azurecr.io/rag-assistant:1.0.0
```

**`rag-assistant:1.0.0` is never built — it is the same image under a second
name.** There is exactly one `docker build` for production, in §6.1;
`docker tag` adds a name, it does not rebuild anything. The full chain, and what
the image is called at each step:

| Step | Command | Name |
|---|---|---|
| §6.1 build | `docker build -t rag-assistant:prod --build-arg BAKE_INDEX=false .` | `rag-assistant:prod` |
| §6.2 tag | `docker tag rag-assistant:prod acrragprod.azurecr.io/rag-assistant:1.0.0` | *both* names, one image |
| §6.2 push | `docker push acrragprod.azurecr.io/rag-assistant:1.0.0` | in the registry |
| §6.4 / §6.5 | `--image acrragprod.azurecr.io/rag-assistant:1.0.0` | pulled from the registry |

`docker images` after the tag shows two rows sharing one IMAGE ID — that is the
same image, not a copy.

Two parts of that name are doing different jobs. `acrragprod.azurecr.io/` is the
registry, and it is what makes the name pushable — Docker infers the registry
from the name, so a tag without it would try to push to Docker Hub. `1.0.0` is a
version **you choose**; nothing derives it from the repo.

**If you bump the version, bump it everywhere.** The literal appears in five
runnable places — the `tag` and `push` above, the ingest-Job YAML in §6.3, the
Job in §6.4, and the deploy in §6.5 — and they must all agree, or you will deploy
an image you did not just push:

```powershell
Select-String -Path Deployment.md -Pattern 'rag-assistant:1\.0\.0'
```

They are written out rather than held in a `$Version` variable on purpose: each
block is meant to run on its own, and a variable set in this fence would be empty
in a later one.

Use a version, not `latest` — Container Apps revisions are how you roll back, and
they need distinguishable images.

### 6.3 Load the index — and where the corpus comes from

**Where do the documents live in production?** Today: **inside the image**. The
Dockerfile copies `KnwoledgeBaseDocuments/` into the runtime stage, so every
image — `demo` and `prod` alike — carries the corpus. There is **no Azure Storage
integration in this repository**; `RAG_SOURCE_DIR` is a filesystem path, and
nothing reads from Blob Storage.

Three ways to get documents to production, with what is actually built:

| Option | How it works | Status |
|---|---|---|
| **Ingest from a trusted machine** (below) | Your local checkout is the source. The **documents never leave your machine** — only chunks and vectors are written to Azure AI Search | ✅ **Implemented, and the default.** The right answer for a corpus that changes rarely |
| **Azure Files mount** | Put the corpus on a file share, mount it into the ingest Job, and point `RAG_SOURCE_DIR` at the mount. The corpus then lives in Azure and is **not** baked into the image | ⚠️ **Needs no code** — `RAG_SOURCE_DIR` already accepts any path — but it is **documented, not tested**. See below |
| **Blob → Event Grid → Queue → Function** | [architecture.md](docs/architecture.md#target-architecture), [ingestion-flow.md](docs/ingestion-flow.md) | 🔲 **Not implemented.** The design target; no code exists |

**The consequence of baking, stated plainly:** the scheduled ingest Job in §6.4
re-indexes whatever was in the image *at build time*. Adding or editing a document
therefore means rebuilding and redeploying the image — unless you take the Azure
Files route, which decouples the two.

#### Option 1 — ingest from a trusted machine

Run ingestion **once**, against the production search service:

```powershell
$SearchName = "<name>"

$env:RETRIEVER_BACKEND     = "azure"
$env:AZURE_SEARCH_ENDPOINT = "https://$SearchName.search.windows.net"
$env:AZURE_SEARCH_API_KEY  = az search admin-key show `
    --service-name $SearchName -g $ResourceGroup --query primaryKey -o tsv

$env:AZURE_OPENAI_ENABLED              = "true"
$env:AZURE_OPENAI_ENDPOINT             = "https://<aoai>.openai.azure.com"
$env:AZURE_OPENAI_API_KEY              = "<key>"
$env:AZURE_OPENAI_EMBEDDING_DEPLOYMENT = "text-embedding-3-small"

python scripts/ingest.py --force
```

Each variable gets its own line — PowerShell has no equivalent of bash's
`export A=1 B=2 C=3` on one statement.

Note what crosses the network here: the parser and chunker run locally, so only
chunk text and vectors are sent to Azure AI Search (and chunk text to Azure
OpenAI for embedding). The source files stay put.

#### Option 2 — Azure Files, so the corpus is not baked in

Upload the corpus to a file share, register the share with the Container Apps
environment, and point `RAG_SOURCE_DIR` at the mount. No code changes — the
setting already accepts any path.

```powershell
$Storage = "stragcorpus"

az storage account create -g $ResourceGroup -n $Storage -l $Location --sku Standard_LRS
$StorageKey = az storage account keys list -g $ResourceGroup -n $Storage --query "[0].value" -o tsv
az storage share create --account-name $Storage --account-key $StorageKey --name corpus

# upload the documents
az storage file upload-batch --account-name $Storage --account-key $StorageKey `
    --destination corpus --source KnwoledgeBaseDocuments

# make the share available to the Container Apps environment
az containerapp env storage set -g $ResourceGroup -n cae-rag-prod `
    --storage-name corpusmount --access-mode ReadOnly `
    --account-name $Storage --azure-file-account-key $StorageKey --azure-file-share-name corpus
```

**Mounting it needs YAML.** Neither `az containerapp create` nor
`az containerapp job create` has a `--volume` flag — only `--secret-volume-mount`,
which is for secrets. The volume has to come in through `--yaml`:

```yaml
properties:
  template:
    containers:
      - name: rag-ingest
        image: acrragprod.azurecr.io/rag-assistant:1.0.0
        command: ["python"]
        args: ["scripts/ingest.py"]
        env:
          - name: RAG_SOURCE_DIR
            value: /mnt/corpus
          - name: RETRIEVER_BACKEND
            value: azure
        volumeMounts:
          - volumeName: corpus
            mountPath: /mnt/corpus
    volumes:
      - name: corpus
        storageType: AzureFile
        storageName: corpusmount
```

```powershell
az containerapp job create -g $ResourceGroup -n job-rag-ingest `
    --environment cae-rag-prod --yaml ingest-job.yaml
```

With this in place, updating the knowledge base is a file upload rather than an
image rebuild. **Documented, not tested** — like everything else from §6.2 onward.

### 6.4 Where ingestion runs — and where it must not

**Do not run ingestion inside the API container.** The manifest
(`data/manifest.improved.json`) is local state that records what has already
been indexed; on an ephemeral filesystem it disappears on every restart, and
with it the ability to detect deleted documents. Three options, worst to best:

1. **Manual** (above) — fine while the corpus changes rarely.
2. **A Container Apps Job** on a schedule, with the manifest on an Azure Files
   mount so it survives between runs:
   ```powershell
   az containerapp job create -g $ResourceGroup -n job-rag-ingest `
     --environment cae-rag-prod --trigger-type Schedule `
     --cron-expression "0 */6 * * *" `
     --image acrragprod.azurecr.io/rag-assistant:1.0.0 `
     --command "python" --args "scripts/ingest.py"
   ```

   Keep the cron expression quoted — unquoted, PowerShell would try to expand
   the `*` characters as wildcards against the current directory.

   **As written, this Job re-indexes the corpus baked into the image**, so it
   picks up document changes only when the image is rebuilt. To have it read a
   corpus you can update independently, mount a file share and set
   `RAG_SOURCE_DIR` — the YAML form in [§6.3 Option 2](#63-load-the-index--and-where-the-corpus-comes-from),
   which also covers why the manifest wants a mount of its own.
3. **Event-driven**, as [docs/architecture.md](docs/architecture.md) specifies:
   Blob Storage → Event Grid → Queue → Function, with the manifest in Cosmos DB.
   This is the design target; it is not implemented here.

### 6.5 Deploy

```powershell
az containerapp create `
  -g $ResourceGroup -n ca-rag-assistant --environment cae-rag-prod `
  --image acrragprod.azurecr.io/rag-assistant:1.0.0 `
  --registry-server acrragprod.azurecr.io `
  --system-assigned `
  --ingress external --target-port 8000 --transport auto `
  --min-replicas 1 --max-replicas 10 `
  --cpu 1.0 --memory 2.0Gi `
  --env-vars `
     RETRIEVER_BACKEND=azure `
     RAG_PROFILE=improved `
     AZURE_SEARCH_ENDPOINT=https://<name>.search.windows.net `
     AZURE_SEARCH_INDEX=northwind-kb `
     AZURE_OPENAI_ENABLED=true `
     AZURE_OPENAI_ENDPOINT=https://<aoai>.openai.azure.com `
     AZURE_OPENAI_CHAT_DEPLOYMENT=gpt-4o `
     AZURE_OPENAI_UTILITY_DEPLOYMENT=gpt-4o-mini `
     AZURE_OPENAI_EMBEDDING_DEPLOYMENT=text-embedding-3-small `
     API_ALLOW_ANONYMOUS=false `
     API_ALLOWED_ORIGINS=https://chat.northwindtraders.example `
     LOG_FORMAT=json
```

**Check for trailing spaces if this block fails to parse.** A backtick is only a
continuation when it is the *last* character on the line; one stray space after
it and PowerShell ends the command there, sending `az` roughly half its
arguments.

`--min-replicas 1` rather than 0: scale-to-zero means the first question after
an idle period pays a cold start that includes loading the index.

**Two settings that are security-critical and easy to forget.**
`API_ALLOW_ANONYMOUS` defaults to `true` for local development — leave it and
your production endpoint answers unauthenticated requests with unrestricted
department scope. `API_ALLOWED_ORIGINS` defaults to `http://localhost:8000`;
set it to the real origin or the browser will block the UI.

### 6.6 Secrets and identity

Never pass keys with `--env-vars`; they are visible in the revision definition.

```powershell
# grant the app's identity access, then reference secrets from Key Vault
$Principal = az containerapp show -g $ResourceGroup -n ca-rag-assistant `
    --query identity.principalId -o tsv

az role assignment create --assignee $Principal `
  --role "Search Index Data Reader" --scope "<search-resource-id>"
az role assignment create --assignee $Principal `
  --role "Cognitive Services OpenAI User" --scope "<aoai-resource-id>"

az containerapp secret set -g $ResourceGroup -n ca-rag-assistant `
  --secrets "search-key=keyvaultref:https://kv-rag-prod.vault.azure.net/secrets/search-key,identityref:system"
az containerapp update -g $ResourceGroup -n ca-rag-assistant `
  --set-env-vars AZURE_SEARCH_API_KEY=secretref:search-key
```

The application authenticates with API keys today. Moving to Managed Identity
end-to-end means swapping the `api-key` header for a bearer token in
[`src/rag/providers/http.py`](src/rag/providers/http.py) — the role assignments
above are the half that is already correct.

### 6.7 Probes and scaling

```powershell
az containerapp update -g $ResourceGroup -n ca-rag-assistant `
  --scale-rule-name concurrency --scale-rule-type http `
  --scale-rule-http-concurrency 20
```

Probe configuration, via YAML on the container:

| Probe | Use | Why |
|---|---|---|
| Readiness | `httpGet /health`, period 10 s | Returns **503 while the index is empty**, so a replica takes traffic only once it can actually answer |
| Liveness | **TCP on 8000**, not `/health` | `/health` returning 503 for an empty index is correct readiness behaviour and catastrophic liveness behaviour — it would restart-loop a healthy process forever |
| Startup | `httpGet /health`, failureThreshold 12 | Loading the index takes a few seconds |

Scaling on HTTP concurrency rather than CPU is deliberate: the service spends
most of a request waiting on Azure OpenAI, so CPU stays low while request
latency climbs, and a CPU rule would never fire.

---

## 7. Configuration reference

Every variable, its default and its effect. All are optional — with none set
the app runs fully locally and says so.

### Azure OpenAI

| Variable | Default | Effect |
|---|---|---|
| **`AZURE_OPENAI_ENABLED`** | **`false`** | **Master switch. Without it the app is fully local no matter what other variables are set** |
| `AZURE_OPENAI_ENDPOINT` | — | Resource endpoint |
| `AZURE_OPENAI_API_KEY` | — | API key |
| `AZURE_OPENAI_API_VERSION` | `2024-10-21` | Data-plane API version |
| `AZURE_OPENAI_CHAT_DEPLOYMENT` | falls back to `AZURE_OPENAI_DEPLOYMENT_NAME` | Answer synthesis |
| `AZURE_OPENAI_UTILITY_DEPLOYMENT` | chat deployment | Rerank / condense / verify / judge. **Point at a mini model** |
| `AZURE_OPENAI_EMBEDDING_DEPLOYMENT` | — | Unset ⇒ local hashed-feature embedder |
| `AZURE_OPENAI_EMBEDDING_DIMENSIONS` | `1536` | Corrected from a live probe if it differs |

### Azure AI Search

| Variable | Default | Effect |
|---|---|---|
| `RETRIEVER_BACKEND` | `local` | `local` or `azure` |
| `AZURE_SEARCH_ENDPOINT` | — | Service endpoint |
| `AZURE_SEARCH_API_KEY` | — | Admin key (needed to create the index) |
| `AZURE_SEARCH_INDEX` | `northwind-kb` | Index name |
| `AZURE_SEARCH_API_VERSION` | `2024-07-01` | REST API version |
| `AZURE_SEARCH_SEMANTIC` | `true` | Semantic ranker. Needs Basic SKU or above |

### TLS

| Variable | Default | Effect |
|---|---|---|
| `AZURE_CA_BUNDLE` | — | Path to a corporate root CA |
| `AZURE_USE_SYSTEM_CERTS` | `false` | Use the OS trust store |
| `AZURE_TLS_VERIFY` | `true` | `false` disables verification. **Development only** |

### Concurrency

| Variable | Default | Effect |
|---|---|---|
| `AZURE_HTTP_MAX_CONNECTIONS` | `200` | Connection pool per provider. One question fans out to up to 3 concurrent searches, so a replica holding N questions wants ~3N. Too low and the pool becomes the bottleneck — and the readiness probe queues behind query traffic |
| `AZURE_EMBED_CONCURRENCY` | `8` | Embedding batches in flight during ingestion. The back-pressure valve against Azure OpenAI TPM quota; raising it past what the quota allows produces 429s, not throughput |

### Behaviour

| Variable | Default | Effect |
|---|---|---|
| `RAG_PROFILE` | `improved` | `improved` or `baseline` (the naive comparison system) |
| `RAG_SOURCE_DIR` | `KnwoledgeBaseDocuments` | Corpus location |
| `RAG_DATA_DIR` | `data` | Index, manifest and embedding cache |
| `RAG_RETRIEVE_TOP_K` | `20` | Candidates fetched before reranking |
| `RAG_CONTEXT_TOP_K` | `5` | Chunks placed in the prompt. Lower ⇒ cheaper, worse multi-hop |
| `RAG_MIN_RELEVANCE` | `4.0` | Rerank score below which the system abstains. Raise ⇒ more refusals |
| `RAG_MAX_CONTEXT_CHARS` | `12000` | Context budget |
| `RAG_ENABLE_ANSWER_CACHE` | `true` | Single-turn answer cache, scoped per department |

### API and observability

| Variable | Default | Effect |
|---|---|---|
| `API_ALLOWED_ORIGINS` | `http://localhost:8000` | CORS allow-list, comma-separated |
| `API_ALLOW_ANONYMOUS` | `true` | **Set `false` in production** |
| `LOG_LEVEL` | `INFO` | Standard levels |
| `LOG_FORMAT` | `json` | `json` or `text` |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | — | Reserved; the exporter is a stub |

---

## 8. Operations

**When documents change.** Re-run `python scripts/ingest.py`. It is incremental:
unchanged documents are neither parsed nor embedded, modified documents have
their previous chunks purged before new ones land, and deleted documents are
removed. Re-running with no changes makes zero embedding calls. Details in
[docs/ingestion-flow.md](docs/ingestion-flow.md).

**Cache invalidation.** `POST /api/v1/ingest` clears the answer cache when
anything was written or purged. External ingestion (§6.4) does not — restart the
revision, or accept up to 256 stale cached answers.

**Observability.** Every request carries a correlation ID, returned in
`X-Correlation-Id` and present on every log line. Every answer carries a
per-stage trace:

```
condense · decompose · embed · search · version_filter · rerank
         · version_rank · context · generate · verify
```

With `LOG_FORMAT=json` these land in Log Analytics as structured fields. The
metrics worth alerting on: abstention rate (a jump means retrieval broke),
groundedness score, p95 latency by stage, and tokens per question by department.

**Cost control**, in descending order of effect: point
`AZURE_OPENAI_UTILITY_DEPLOYMENT` at a mini model; enable response caching at
API Management; lower `RAG_CONTEXT_TOP_K`; reduce embedding dimensions; set
per-department quotas at the gateway.

---

## 9. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Answers are extractive though credentials are set | `AZURE_OPENAI_ENABLED` is not `true` — by design | Set `AZURE_OPENAI_ENABLED=true`; the startup warning names this exactly |
| Unexpected Azure OpenAI spend from a "local" run | Inherited `AZURE_OPENAI_*` variables plus `AZURE_OPENAI_ENABLED=true` | Unset the flag; `GET /health` reports `azure_openai_enabled` |
| `ModuleNotFoundError: No module named 'pydantic_core._pydantic_core'` | The venv's interpreter and its compiled packages are different Python versions — usually `python -m venv` run over an existing venv | Compare `.venv\pyvenv.cfg` with the `cpNNN` tag on `.venv\Lib\site-packages\pydantic_core\*.pyd`; delete `.venv` and rebuild — §2.2 |
| `uv pip install`: `invalid peer certificate: UnknownIssuer` | TLS-inspecting proxy | `uv pip install --native-tls -r requirements.txt` |
| `ModuleNotFoundError: No module named 'rag'` | You ran `python -m rag.cli`; the package is under `src/`, which `-m` does not put on `sys.path` | Use `python scripts/cli.py …`, or `pip install -e .` first — §2.4 |
| `ingest.py` prints `0 new … 11 unchanged` | Nothing is wrong — ingestion is incremental and the corpus has not changed | Check `127 total (22 tables)`. `--force` rebuilds — §3 |
| `warning: The fitz API is deprecated` | PyMuPDF ≥ 1.28.2 with the old import name | Fixed in this repo; `git pull` if you still see it |
| PowerShell: `Activate.ps1 cannot be loaded` | Script execution policy | `Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned` — §2.2 |
| `CERTIFICATE_VERIFY_FAILED` on every Azure call | TLS-inspecting proxy with a CA Python does not trust | §5.3 |
| `az` fails with `CERTIFICATE_VERIFY_FAILED` though the app works | The CLI has its own trust store and ignores `AZURE_CA_BUNDLE` | Set `REQUESTS_CA_BUNDLE` as well — §5.3 |
| `ServiceModelDeprecated` from `az … deployment create` | A pinned `--model-version` that Azure has since retired | Let §5.2's `Resolve-ModelVersion` pick one; `az cognitiveservices account list-models` shows what is deployable |
| A deployment that exists is created again, or `az … create` runs unexpectedly | `if (az … -o none)` tests *output*, not the exit code, so it is always false | Use `Test-AzExists` from §5.2 |
| `/health` returns 503, UI says "index is empty" | No index built, or `RAG_DATA_DIR` points elsewhere | `python scripts/ingest.py` |
| `DeploymentNotFound` | Deployment name ≠ model name; they are independent | `az cognitiveservices account deployment list` |
| Answers are extractive, not written | No reachable chat deployment | Check the startup log; it names the provider |
| Retrieval quality poor, log says `local-hashing` | No embedding deployment | Deploy `text-embedding-3-small` and re-ingest |
| `embedding dimension changed … will be dropped` | Embedding model changed under an existing index | `python scripts/ingest.py --force` |
| `Semantic search is not enabled for this service` | Free-tier Search | `AZURE_SEARCH_SEMANTIC=false`, or upgrade to Basic |
| Container unreachable through ingress | uvicorn bound to loopback | `--host 0.0.0.0` (already in the image `CMD`) |
| Replicas restart-looping | `/health` used as a liveness probe | Use TCP for liveness — §6.7 |
| Deleted documents still answered | Manifest lost between ingest runs | Persist `RAG_DATA_DIR` — §6.4 |
| Anyone can query any department | `API_ALLOW_ANONYMOUS` left at `true` | Set `false` |

---

## 10. What is not included

Stated plainly so nothing here is mistaken for more than it is.

- **The image is verified; the Azure deployment is not.** Both build variants
  were built and run, and the container was exercised with and without Azure
  credentials (§6.1). What has *never* run is everything from `az containerapp
  create` onward — no subscription was deployed to. Those commands follow the
  documented CLI surface but should be treated as a first draft, particularly
  the probe and secret-reference syntax, which Azure changes more often than the
  core create flags.
- **No IaC and no CI/CD.** §6 is `az` CLI steps. A real deployment wants Bicep
  (Azure Verified Modules) or Terraform for the resources, and a pipeline that
  builds, runs `verify_pipeline.py` and `verify_lifecycle.py`, gates on the
  evaluation's hallucination rate, and deploys a revision.
- **Authentication is a development stub.**
  [`src/rag/api/auth.py`](src/rag/api/auth.py) maps static bearer tokens to
  department scopes. The *authorization* half — claims driving a pre-filter
  inside the search query — is production-shaped; the *authentication* half must
  be replaced with Entra ID JWT validation.
- **Azure AI Search is contract-verified, not integration-verified.** §5.2 has
  the checklist to run once a real service exists.
- **Application Insights export is a stub.** The spans and correlation IDs
  exist; the exporter does not.
- **No autoscaling proof.** The concurrency rule in §6.7 is reasoned from where
  request time goes, not load-tested.
