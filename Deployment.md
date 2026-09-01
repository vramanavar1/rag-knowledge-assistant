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
- Docker, for §6 — **only if you build locally**. §6.1 offers `az acr build`,
  which builds and pushes in Azure, so §6 is completable with `az` alone.

#### What permissions you need

**Two different identities are involved, and they need different things.**
Confusing them is the usual reason a deploy stalls:

| Identity | Needs | Where it bites |
|---|---|---|
| **You** — the account `az login` used | `Contributor` on the resource group to create resources, **plus `Owner` or `User Access Administrator`** to run `az role assignment create` | **`Contributor` cannot create role assignments.** Creating a resource and granting access to it are separate privileges, and §6.5 needs both |
| **The container app's managed identity** | `AcrPull` on the registry | Without it the deploy reports `No credential was provided to access Azure Container Registry`. That message is about *the app*, not about you |

Check what you have before starting §6:

```powershell
az account show --query "{subscription:name, user:user.name}" -o table

az role assignment list --assignee (az account show --query user.name -o tsv) `
    --all --include-groups --include-inherited -o table
```

**Both flags on the second command matter.** A role can be inherited from a
management group or granted through a group you belong to; without
`--include-groups --include-inherited` the listing can come back empty while you
are in fact an Owner.

If you have `Contributor` but not `Owner` / `User Access Administrator`,
everything in §6 works except the one `az role assignment create` line — see
§6.5 for how to hand that single command to someone who can run it, and §6.6 for
the fallback that needs no role assignment at all.

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

### 4.1 Pipeline coverage — 54 checks, no network

```powershell
python scripts/verify_pipeline.py
```

Walks all nine stages of the assignment's pipeline against a throwaway copy of
the corpus and asserts each. Expect `9/9 stages verified, 53 checks, 0
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

**The service and the index are two different things, and they have different
names.** `AZURE_SEARCH_ENDPOINT` names the *service* — the Azure resource you
provisioned, `srch-northwind-kb` here. `AZURE_SEARCH_INDEX` names an *index
inside it*, `northwind-kb`. One service holds many indexes, so these are not
interchangeable and the index should **not** be renamed to match the service. If
you see

```
The index 'northwind-kb' for service 'srch-northwind-kb' was not found
```

the service was found and answered — it is the index that does not exist yet.
`ingest.py` creates it; see [§9](#9-troubleshooting).

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

**Nothing can pull the image until an identity is allowed to.** The registry is
created with admin access off (§6.2), so there is no password to fall back on,
and the app's own system-assigned identity does not exist yet at the moment of
the first pull. §6.5 therefore creates a user-assigned identity, grants it
`AcrPull`, and passes it to `az containerapp create` — do that step or the deploy
stops at `No credential was provided to access Azure Container Registry`.

**§6 calls `az` directly**, unlike §5.2, whose `Test-AzExists` / `Invoke-Az`
helpers exist only within that block. Each step here is meant to be read and run
one at a time. If you want the same fail-fast behaviour, paste §5.2's two helper
functions into the session first and prefix the mutating calls with `Invoke-Az` —
but do not assume they are already defined. On Windows PowerShell 5.1 this
matters more than it looks: with `$ErrorActionPreference = "Stop"` a mere `az`
*warning* terminates the command, so check whether a step actually failed before
re-running it.

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

#### Or build in Azure instead — `az acr build`

`az acr build` uploads the build context to Azure Container Registry, builds it
there, and **pushes on success**. It replaces the whole `docker build` → `az acr
login` → `docker tag` → `docker push` chain with one command, and needs no local
Docker at all:

```powershell
# --- the §6 preamble. Re-paste it in any new shell; it is deterministic. ---
$Acr   = "acrragprodkb"
$Sha   = git rev-parse --short HEAD
$Image = "$Acr.azurecr.io/rag-assistant:$Sha"

if (git status --porcelain) {
    Write-Warning "uncommitted changes - '$Sha' will not describe what you build"
}

# same prod image, built on an ACR agent and pushed in one step
az acr build -r $Acr -t rag-assistant:$Sha --build-arg BAKE_INDEX=false .

# the demo variant, for symmetry -- Path C does not use it
az acr build -r $Acr -t rag-assistant:demo --build-arg BAKE_INDEX=true .
```

**The tag is the commit, and that is the point.** An image called `1.0.0` cannot
tell you which build it holds — rebuild it and two revisions carry the same
label over different code, which is how a deploy comes to look like it did
nothing. `$Sha` names exactly one commit, so the answer to "what is running?" is
readable straight off the revision list.

**A SHA tag on a dirty tree lies**, hence the guard: the build would contain
uncommitted work that the commit does not. Commit first, or treat the tag as
approximate and pin by digest (§8) when it has to be provable.

These three lines are the preamble for the rest of §6. Every block below assumes
`$Acr`, `$Sha` and `$Image`; re-paste them if you open a new shell.

> **These belong to §6.2, not here.** `docker build` is purely local, so it runs
> at this point in the document. `az acr build` needs the registry to already
> exist, and `acrragprodkb` is not created until `az acr create` in §6.2. Run them
> after that step; they are shown here so the two routes can be compared
> side by side.

**On Windows, set the console encoding first.** `az acr build` streams the build
log to your terminal, and a default Windows console (cp437 or cp1252) cannot
encode every character that log may contain — the command then dies with
`UnicodeEncodeError: 'charmap' codec can't encode characters`:

```powershell
$env:PYTHONIOENCODING = "utf-8"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
```

**That error does not mean the build failed.** The crash is in the CLI printing
the log; the task carries on running in Azure and usually finishes and pushes.
Never re-run a build on the strength of it — ask the registry what actually
happened:

```powershell
az acr task list-runs -r $Acr --top 5 -o table
az acr repository show-tags -n $Acr --repository rag-assistant -o table
```

`--no-logs` avoids the streaming path altogether, at the cost of no longer
blocking until the build finishes. §9 has the full entry.

| | `docker build` (§6.1 + §6.2) | `az acr build` (§6.2) |
|---|---|---|
| Local Docker daemon | Required | **Not needed** |
| Steps to registry | 4 — build, login, tag, push | **1** |
| Where it builds | Your machine | An Azure-hosted agent |
| Image architecture | Whatever your machine is | Always `linux/amd64` |
| Registry must exist first | No | **Yes** |
| What crosses the network | The built image, hundreds of MB | The build context, ~2 MB here |

Two differences matter beyond convenience:

**`-t` is relative to the registry.** `-t rag-assistant:$Sha` resolves to
`$Acr.azurecr.io/rag-assistant:$Sha` — which is exactly `$Image`, the name §6.4
and §6.5 pull.
There is no FQDN to get wrong and no separate `docker tag` step that can drift
out of step with the deploy.

**It always produces `linux/amd64`.** A local `docker build` on an ARM
workstation yields an arm64 image that Container Apps cannot run, and nothing
catches it until the container tries to start. ACR Tasks builds on a Linux amd64
agent unless `--platform` says otherwise.

`--admin-enabled false` is no obstacle: `az acr build` authenticates through ARM
with your `az login` identity, the same way `az acr login` does in §6.2. What it
needs is `AcrPush` on the registry — **your** permission, not the app's `AcrPull`.
See §2.6, which is where the two identities are told apart.

`BAKE_INDEX` behaves identically on the ACR agent — no Azure credentials reach
the build environment there either, so the note below applies unchanged.

**Give them different tags.** Both `docker build` commands above previously wrote
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
if ($PSVersionTable.PSVersion.Major -ge 7) {
    # 7.3+: makes a non-zero az *exit code* fatal, which is what we want.
    $PSNativeCommandUseErrorActionPreference = $true
} else {
    # Windows PowerShell 5.1 has no such variable, and "Stop" there makes any az
    # *warning* a terminating error -- killing commands that actually worked.
    $ErrorActionPreference = "Continue"
}

$ResourceGroup = "rg-rag-prod"
$Location      = "westus"
$Acr           = "acrragprodkb"        # same value as §6.1's preamble

az group create -n $ResourceGroup -l $Location

# Search + model deployments: run the §5.2 block with $ResourceGroup set to the
# value above, and $OpenAiName set to your Azure OpenAI resource.

# Registry, Key Vault, observability, Container Apps environment
az acr create -g $ResourceGroup -n $Acr --sku Basic --admin-enabled false
az keyvault create -g $ResourceGroup -n kv-rag-prod
az monitor app-insights component create `
  -g $ResourceGroup -a appi-rag-prod -l $Location --workspace "<log-analytics-id>"
az containerapp env create -g $ResourceGroup -n cae-rag-prod -l $Location
```

**`acrragprodkb` is almost certainly taken.** Registry names are **globally
unique across all of Azure**, not merely unique within your subscription, and
`az acr create` fails with `already in use` if someone else has claimed it. Pick
your own — 5–50 characters, lowercase alphanumeric only, no hyphens.

If you change it, change the **`$Acr` assignments** — every command takes the
registry from that variable, so the literal only appears where `$Acr` is set (the
§6.1 preamble, §6.2, and the standalone blocks in §6.5, §6.6 and §8) plus the
prose that names it. Let the search find them:

```powershell
Select-String -Path Deployment.md -Pattern 'acrragprodkb'
```

**The preamble is shell-aware on purpose.** The two editions treat a failing
native command in opposite ways, so no single setting is right for both:

| | Windows PowerShell 5.1 | PowerShell 7 |
|---|---|---|
| `$ErrorActionPreference = "Stop"` on `az` | **Any stderr write is fatal — warnings included** | Ignored for native commands |
| `$PSNativeCommandUseErrorActionPreference` | Does not exist (7.3+ only) | Makes a non-zero **exit code** fatal |

With the branch above, an `az` *warning* never kills the run on either shell —
which matters, because several `az containerapp` operations warn routinely. The
trade-off is stated plainly: **on 5.1 a genuine `az` failure will not stop the
script either.** Check `$LASTEXITCODE` after anything that matters, or run §6 in
PowerShell 7, where a non-zero exit still throws.

**`--admin-enabled false` is deliberate, and it has a consequence to plan for.**
It means the registry has no username/password, so Container Apps cannot fall
back on one and **must** pull with a managed identity that holds `AcrPull`. That
identity has to exist *before* the app does, which is why §6.5 creates one rather
than relying on the app's own system-assigned identity. Skip it and the deploy
reports `No credential was provided to access Azure Container Registry`.

Push the image — `az acr login` uses **your** credentials, which is separate from
how the *app* authenticates later:

```powershell
az acr login -n $Acr
docker tag rag-assistant:prod $Image
docker push $Image
```

**Or skip all three, and §6.1's build with them.** The registry now exists, so
`az acr build` can run — it builds on an Azure agent and pushes on success:

```powershell
az acr build -r $Acr -t rag-assistant:$Sha --build-arg BAKE_INDEX=false .
```

On Windows, set `$env:PYTHONIOENCODING = "utf-8"` before running it — see §6.1.
Without it the command can die with `UnicodeEncodeError` **while the build keeps
running in Azure**, which reads as a build failure and is not one.

That is the whole route: no local Docker daemon, no `az acr login`, no `docker
tag`, no `docker push`. It authenticates through ARM with the identity `az login`
established, so `--admin-enabled false` does not stand in its way — but it does
need **`AcrPush` on the registry**, which is a permission held by *you*, not by
the app's managed identity. §2.6 has the distinction.

Everything downstream is unchanged: the image lands under the same name, and
§6.3 through §6.5 neither know nor care which route produced it.

**On the `docker` route, `$Image` is never built — it is the same image under a
second name.** There is exactly one `docker build` for production, in §6.1;
`docker tag` adds a name, it does not rebuild anything. The full chain, and what
the image is called at each step:

| Step | Command | Name |
|---|---|---|
| §6.1 build | `docker build -t rag-assistant:prod --build-arg BAKE_INDEX=false .` | `rag-assistant:prod` |
| §6.2 tag | `docker tag rag-assistant:prod $Image` | *both* names, one image |
| §6.2 push | `docker push $Image` | in the registry |
| §6.4 / §6.5 | `--image $Image` | pulled from the registry |

`docker images` after the tag shows two rows sharing one IMAGE ID — that is the
same image, not a copy.

**On the `az acr build` route that is not true**, and the difference is worth
holding on to: there is no intermediate `rag-assistant:prod`, because the
commit-tagged name *is* the build target. The first three rows collapse into one:

| Step | Command | Name |
|---|---|---|
| §6.2 build + push | `az acr build -r $Acr -t rag-assistant:$Sha --build-arg BAKE_INDEX=false .` | in the registry |
| §6.4 / §6.5 | `--image $Image` | pulled from the registry |

Nothing lands in your local `docker images` on this route — the image only ever
exists in the registry.

Two parts of that name do different jobs. `$Acr.azurecr.io/` is the registry, and
it is what makes the name pushable — Docker infers the registry from the name, so
a tag without it would try to push to Docker Hub. `$Sha` is the **identity**: it
says which commit this image was built from.

**There is nothing to bump.** Every command in §6 takes its image from `$Image`,
so the version exists in exactly one place — the preamble — and it is computed,
not chosen. This replaces an earlier scheme where a literal `1.0.0` appeared in
nine runnable places and every one had to be edited in step. That scheme failed
in the way such schemes do: a rebuilt tag left two revisions carrying one label
over different code, and working out which was deployed meant comparing traceback
line numbers against the repository.

An earlier note in this section argued against a variable, on the grounds that
each block should run on its own and a variable set in one fence is empty in the
next. That objection applied to a *chosen* version like `$Version`, which you
cannot reconstruct without remembering it. `$Sha` is derived from the repository,
so re-pasting the three preamble lines in a fresh shell always reproduces it, and
the blocks stay independently runnable.

Use a commit, not `latest` and not a hand-picked number — Container Apps
revisions are how you roll back, and they need images that can be told apart. A
commit id is the strongest form of that: it is unique per build *and* it points
back at the source.

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
        image: __IMAGE__
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

**`__IMAGE__` is a placeholder, because YAML cannot read a shell variable.**
Render it before applying, so the Job and the API provably run the same build —
a Job left on an older image re-indexes with a different embedder, which is the
mismatch of §9 arriving by the back door:

```powershell
(Get-Content ingest-job.yaml -Raw).Replace('__IMAGE__', $Image) |
    Set-Content ingest-job.resolved.yaml

az containerapp job create -g $ResourceGroup -n job-rag-ingest `
    --environment cae-rag-prod --yaml ingest-job.resolved.yaml
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
     --image $Image `
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

**Grant registry access first.** §6.2 created the registry with
`--admin-enabled false`, which is the right posture but means there is no
username/password for Container Apps to fall back on. If you deploy without
arranging an identity, `az` reports

```
WARNING: No credential was provided to access Azure Container Registry.
Trying to look up credentials...
```

and the pull fails, because there is nothing to look up.

A **system-assigned** identity cannot solve this on its own: it does not exist
until the app is created, so it cannot hold `AcrPull` at the moment the first
image pull happens. `az containerapp registry set --help` says so plainly — *"the
managed identity should have been assigned acrpull permissions"*. Use a
**user-assigned** identity, which can be given the role before anything needs it:

```powershell
$Acr = "acrragprodkb"

# 1. an identity that exists before the app does
az identity create -g $ResourceGroup -n id-rag-assistant
$IdentityId = az identity show -g $ResourceGroup -n id-rag-assistant `
    --query id -o tsv
$IdentityPrincipal = az identity show -g $ResourceGroup -n id-rag-assistant `
    --query principalId -o tsv

# 2. let it pull from the registry -- scoped to that registry, nothing wider
$AcrId = az acr show -g $ResourceGroup -n $Acr --query id -o tsv
az role assignment create --assignee $IdentityPrincipal `
    --role AcrPull --scope $AcrId
```

**`az role assignment create` needs `Owner` or `User Access Administrator`** —
`Contributor` is not enough, and this is the step most likely to be refused on a
corporate subscription. Check with the command in [§2.6](#26-optional-for-the-azure-paths-only-5-6)
before you start. If it is refused, note that *only that one line* needs the
elevated right: `az identity create` and everything after it need only
Contributor, so it is reasonable to hand a subscription owner exactly this:

```powershell
az role assignment create --assignee "<identity-principal-id>" `
    --role AcrPull --scope "<acr-resource-id>"
```

and carry on yourself once it is done. If you cannot get it run at all, §6.6 has
a route that needs no role assignment.

Role assignments are eventually consistent; if the first deploy still reports a
pull failure, wait a minute and retry before changing anything.

Then create the app, telling it to authenticate to the registry **as that
identity** rather than with a password:

```powershell
az containerapp create `
  -g $ResourceGroup -n ca-rag-assistant --environment cae-rag-prod `
  --image $Image `
  --registry-server "$Acr.azurecr.io" `
  --registry-identity $IdentityId `
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
     AZURE_OPENAI_EMBEDDING_DIMENSIONS=1536 `
     API_ALLOW_ANONYMOUS=false `
     API_ALLOWED_ORIGINS=https://chat.northwindtraders.example `
     LOG_FORMAT=json `
     SHOW_ENV_VALUES=false
```

**`AZURE_OPENAI_EMBEDDING_DIMENSIONS` must equal the width `northwind-kb` was
built with.** It is passed straight through on every embedding call, so it
decides the length of every query vector; 1536 is `text-embedding-3-small`'s
native width, which is where the number comes from. It is stated here rather than
left to the default because it is the one setting that determines whether a query
can match the index at all — and a deploy command that does not name it is making
a silent claim about a remote artifact.

**Changing the embedding model or width means re-ingesting the corpus *and*
changing this line.** The two are a pair. If they disagree the app now refuses to
start, logging both widths (§9) — which is what makes pinning an explicit value
safe rather than a trap.

**`SHOW_ENV_VALUES=false` is stated even though `false` is already the code
default**, for the same reason `RAG_PROFILE` and `LOG_FORMAT` are: the deploy
should describe the posture it creates rather than rely on one. It carries real
weight here — the flag exposes a configuration report on the public, unauthenticated
`/health` (§7), and the only way to switch it on is `--set-env-vars`, which
**merges**. Once set to `true` it survives every later update, so a deployment
that never states `false` has no way back to it. §6.6 and §6.7 re-assert it for
the same reason.

**`--registry-identity` does two jobs, which is why there is no `--user-assigned`
here.** It attaches the identity to the app *and* tells the app to authenticate
to the registry with it. Adding `--user-assigned $IdentityId` as well makes the
CLI add the same identity twice and warn:

```
WARNING: User identity /subscriptions/…/id-rag-assistant is already assigned to containerapp
```

That warning is harmless in itself — but on Windows PowerShell 5.1 a warning is
enough to terminate the command, so it reads like a failure. One flag, not two.

`--system-assigned` stays. It is a **different** identity, created for the app
itself, and it is the one §6.6 grants Search and OpenAI access to. The two are
not interchangeable: the user-assigned identity pulls the image, the
system-assigned identity is how the running app authenticates to Azure services.

**Check for trailing spaces if this block fails to parse.** A backtick is only a
continuation when it is the *last* character on the line; one stray space after
it and PowerShell ends the command there, sending `az` roughly half its
arguments.

> **On Windows PowerShell 5.1 that ACR warning alone will kill the command.**
> With `$ErrorActionPreference = "Stop"` — which §6.2's preamble sets and which
> is still in effect later in the same session — *any* `az` write to stderr
> becomes a terminating `NativeCommandError`, warnings included. The command may
> have got further than the transcript suggests, so check before re-running:
> `az containerapp show -g $ResourceGroup -n ca-rag-assistant -o table`.
> §5.2's `Invoke-Az` helper exists precisely to contain this; §6 calls `az`
> directly, so either paste that helper in first or run §6 with
> `$ErrorActionPreference = "Continue"`.

`--min-replicas 1` rather than 0: scale-to-zero means the first question after
an idle period pays a cold start that includes loading the index.

**Two settings that are security-critical and easy to forget.**
`API_ALLOW_ANONYMOUS` defaults to `true` for local development — leave it and
your production endpoint answers unauthenticated requests with unrestricted
department scope. `API_ALLOWED_ORIGINS` defaults to `http://localhost:8000`;
set it to the real origin or the browser will block the UI.

**Notice what is *not* in that list: the two API keys.** They are deliberately
absent — keys in `--env-vars` are visible in the revision definition — but the
app cannot reach Azure AI Search without `AZURE_SEARCH_API_KEY`, and it fails
**quietly**: it falls back to an empty local index and answers
`insufficient_evidence` to everything. **§6.6 is not optional; finish it before
testing the app.**

### 6.6 Secrets and identity

Never pass keys with `--env-vars`; they are visible in the revision definition.

Look the scope ids up rather than pasting them — a role assignment against a
mistyped scope fails in ways that are tedious to read:

```powershell
# The names of the two services, and the resource groups they live in.
# Search and Azure OpenAI are often NOT in the container app's resource group --
# §5.2 may well have created them elsewhere -- so these default to
# $ResourceGroup but can be pointed anywhere.
$SearchName     = "srch-northwind-kb"
$OpenAiName     = "oai-vsquarecloud"
$SearchGroup    = $ResourceGroup
$OpenAiGroup    = $ResourceGroup

# The app's own (system-assigned) identity -- the one that queries Azure.
# Not the user-assigned identity from §6.5, which only pulls the image.
$Principal = az containerapp show -g $ResourceGroup -n ca-rag-assistant `
    --query identity.principalId -o tsv

# Resource ids, fetched rather than typed.
$SearchId = az search service show --name $SearchName -g $SearchGroup `
    --query id -o tsv
$OpenAiId = az cognitiveservices account show --name $OpenAiName -g $OpenAiGroup `
    --query id -o tsv

# Fail loudly here rather than on an empty --scope, which reports a confusing
# "role assignment scope is invalid" instead of "you typed the name wrong".
if (-not $Principal) { throw "no system-assigned identity on ca-rag-assistant -- was it created with --system-assigned?" }
if (-not $SearchId)  { throw "search service '$SearchName' not found in '$SearchGroup'" }
if (-not $OpenAiId)  { throw "Azure OpenAI account '$OpenAiName' not found in '$OpenAiGroup'" }

"principal : $Principal"
"search    : $SearchId"
"openai    : $OpenAiId"

az role assignment create --assignee $Principal `
  --role "Search Index Data Reader" --scope $SearchId
az role assignment create --assignee $Principal `
  --role "Cognitive Services OpenAI User" --scope $OpenAiId
```

Note `az search service show` takes `--name`, while `az search admin-key show`
in §5.2 takes `--service-name` — the two subcommands genuinely differ.

Confirm the assignments landed:

```powershell
az role assignment list --assignee $Principal --all -o table
```

#### What actually goes in Key Vault

**Exactly two values.** Everything else §6.5 passes is configuration, not
secrets — endpoints, deployment names, the index name and the boolean flags are
all safe in the revision definition:

| Vault secret | Becomes | Why it is a secret |
|---|---|---|
| `search-key` | `AZURE_SEARCH_API_KEY` | Admin key for Azure AI Search — grants read **and write** on the index |
| `openai-key` | `AZURE_OPENAI_API_KEY` | Calls billable model deployments |

**§6.2 creates the vault but does not put anything in it**, and the app cannot
read it until its identity is allowed to. All three steps are needed:

```powershell
$Vault = "kv-rag-prod"

# 1. Put the two secrets in the vault.
$SearchKey = az search admin-key show --service-name $SearchName `
    -g $SearchGroup --query primaryKey -o tsv
$OpenAiKey = az cognitiveservices account keys list --name $OpenAiName `
    -g $OpenAiGroup --query key1 -o tsv

az keyvault secret set --vault-name $Vault --name search-key --value $SearchKey -o none
az keyvault secret set --vault-name $Vault --name openai-key --value $OpenAiKey -o none

# 2. Let the app's identity READ them. New vaults use RBAC by default, so this
#    is a role assignment, not an access policy.
$VaultId = az keyvault show -n $Vault -g $ResourceGroup --query id -o tsv
az role assignment create --assignee $Principal `
    --role "Key Vault Secrets User" --scope $VaultId

# 3. Reference them, and map them onto the variables the app reads.
az containerapp secret set -g $ResourceGroup -n ca-rag-assistant --secrets `
    "search-key=keyvaultref:https://$Vault.vault.azure.net/secrets/search-key,identityref:system" `
    "openai-key=keyvaultref:https://$Vault.vault.azure.net/secrets/openai-key,identityref:system"

az containerapp update -g $ResourceGroup -n ca-rag-assistant `
    --image $Image `
    --set-env-vars AZURE_SEARCH_API_KEY=secretref:search-key `
                   AZURE_OPENAI_API_KEY=secretref:openai-key `
                   AZURE_OPENAI_EMBEDDING_DIMENSIONS=1536 `
                   SHOW_ENV_VALUES=false
```

**`--image` is here for a reason that is easy to miss.** A revision created by
`--set-env-vars` alone **inherits the image the app already had**, so an update
can apply new configuration to old code and report success. Naming the image on
every update makes the deployed version explicit rather than implied.

> **Until this is done the app is not talking to Azure AI Search at all — and it
> will not tell you loudly.** [`store/factory.py`](src/rag/store/factory.py)
> treats a missing key as "Azure not configured": with `RETRIEVER_BACKEND=azure`
> but no `AZURE_SEARCH_API_KEY` it logs one warning —
> *"falling back to the local backend"* — and carries on with the **local**
> store. The production image is built `BAKE_INDEX=false`, so that local store is
> **empty**, and every question then returns `insufficient_evidence` regardless
> of which department asked.
>
> `GET /health` is the tell: it reports `"backend": "local"` and
> `"documents": 0` instead of `azure-ai-search`. See [§9](#9-troubleshooting).

#### Confirm the keys actually took effect

A revision that never restarted looks exactly like one that did, so check rather
than assume:

```powershell
$Fqdn = az containerapp show -g $ResourceGroup -n ca-rag-assistant `
    --query "properties.configuration.ingress.fqdn" -o tsv
Invoke-RestMethod "https://$Fqdn/health" | ConvertTo-Json -Depth 6
```

| Field | Must be | If it is not |
|---|---|---|
| `index.backend` | `azure-ai-search` | `AZURE_SEARCH_API_KEY` did not take effect — the app is on the empty local store |
| `providers.embeddings` | `azure-openai:text-embedding-3-small` | **`AZURE_OPENAI_API_KEY` did not take effect.** The app is embedding at 768 while the index expects 1536, and every query will fail inside the search backend |
| `index.chunks` | greater than 0 | Ingestion has not run against this index — §6.3 |

**`local-hashing` under `providers.embeddings` is the single clearest signal
something is missing.** The embedding provider needs *all three* of
`AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_KEY` and
`AZURE_OPENAI_EMBEDDING_DEPLOYMENT` — any one absent silently selects the local
embedder. The startup log names whichever is missing:

```
falling back to the local embedder  missing=AZURE_OPENAI_API_KEY
```

The application authenticates with API keys today. Moving to Managed Identity
end-to-end means swapping the `api-key` header for a bearer token in
[`src/rag/providers/http.py`](src/rag/providers/http.py) — the role assignments
above are the half that is already correct.

**If the app already exists without registry access**, you do not need to
recreate it. Grant `AcrPull` to its system-assigned identity and point the
registry at it:

```powershell
$Acr = "acrragprodkb"
$Principal = az containerapp show -g $ResourceGroup -n ca-rag-assistant `
    --query identity.principalId -o tsv
$AcrId = az acr show -g $ResourceGroup -n $Acr --query id -o tsv

az role assignment create --assignee $Principal --role AcrPull --scope $AcrId
az containerapp registry set -g $ResourceGroup -n ca-rag-assistant `
    --server "$Acr.azurecr.io" --identity system
```

`--identity system` uses the app's own identity; a user-assigned identity's
resource id works there too. This is the ordering the user-assigned route in
§6.5 avoids having to unpick — here the identity already exists, so the role can
finally be granted.

**The fallback, when you cannot create a role assignment at all.** Everything
above needs `Owner` or `User Access Administrator` (§2.6). Admin credentials do
not — enabling them needs only `Contributor` on the registry — which makes this
the honest answer for a locked-down subscription rather than merely a shortcut,
and it is what the warning means by *"trying to look up credentials"*:

```powershell
$Acr = "acrragprodkb"
az acr update -n $Acr --admin-enabled true
$AcrUser = az acr credential show -n $Acr --query username -o tsv
$AcrPass = az acr credential show -n $Acr --query "passwords[0].value" -o tsv

az containerapp registry set -g $ResourceGroup -n ca-rag-assistant `
    --server "$Acr.azurecr.io" --username $AcrUser --password $AcrPass
```

**Know what it costs.** That is a long-lived shared credential, it is stored in
the revision definition, it is not tied to any person, and rotating it means
touching every app that uses it — which is exactly what §6.2's
`--admin-enabled false` was avoiding. Treat it as a bridge: get the deployment
working, then move to the managed-identity route above and
`az acr update -n $Acr --admin-enabled false` once someone can grant `AcrPull`.

### 6.7 Probes and scaling

```powershell
az containerapp update -g $ResourceGroup -n ca-rag-assistant `
  --image $Image `
  --scale-rule-name concurrency --scale-rule-type http `
  --scale-rule-http-concurrency 20 `
  --set-env-vars SHOW_ENV_VALUES=false `
                 AZURE_OPENAI_EMBEDDING_DIMENSIONS=1536
```

Neither `--image` nor `--set-env-vars` is about scaling. Every `az containerapp
create`/`update` in this document names the image and re-asserts the same two
settings, so whichever one you ran last, you know which code is deployed, the
`/health` env report (§7) is off, and the embedding width still matches the
index. `--set-env-vars` merges, leaving the other variables untouched.

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
| `AZURE_OPENAI_EMBEDDING_DIMENSIONS` | `1536` | **Must equal the index's vector width.** Checked at startup; a mismatch stops the app — §9 |

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
| `SHOW_ENV_VALUES` | `false` | Adds a redacted `env` block to `/health`. See below |
| `STARTUP_FAIL_MODE` | `unready` | What a violated startup contract does. `unready`: stay up, fail readiness, report at `/health`. `crash`: exit at boot |
| `LOG_ANSWER_TRAIL` | `false` | Adds the question, answer and chunk snippets to the per-request `answer trail` log line. Scores and document ids are logged regardless — §8 |

**`STARTUP_FAIL_MODE` — refusing to serve, two ways.** With
`RETRIEVER_BACKEND=azure` the app checks at boot that the embedder's vector width
matches the index's. On a mismatch it will not answer questions either way; the
setting decides what is left behind to diagnose with:

| | `unready` (default) | `crash` |
|---|---|---|
| Process | stays up | exits; Container Apps restart-loops it |
| Readiness | fails → takes no traffic | no replica at all |
| `/health` | **503** with `startup_errors` + `embedding_decision` | unreachable |
| `/chat` | 503 quoting the reason | unreachable |
| Log stream | works | **cannot attach — no replica** |

`unready` is the default because a crashed container takes the evidence with it:
no `/health`, and `Unable to open a connection to your app` from the log stream.
Both modes are equally safe — neither serves an answer it cannot ground — so the
one that can be interrogated wins. Use `crash` where a failed revision must never
linger, and read the reason from Log Analytics (§8).

**`SHOW_ENV_VALUES` — diagnosing configuration from outside the container.**
Set it to `true` and `/health` gains an `env` block listing every variable the
app reads, **including the ones that are unset**:

```jsonc
"env": {
  "AZURE_OPENAI_ENDPOINT":   "https://aoai-rag.openai.azure.com",
  "AZURE_OPENAI_API_KEY":    "<EMPTY>",                      // set, but resolved to nothing
  "AZURE_SEARCH_API_KEY":    "<set len=32 sha256:9f2a1c7d>", // set, value never shown
  "AZURE_SEARCH_SEMANTIC":   "<UNSET> (default: true)"       // never configured
}
```

The three states are the whole point, and `az containerapp show` cannot
distinguish them: a variable mapped to `secretref:openai-key` displays a *name
but no value*, so a Key Vault reference that fails to resolve — a missing **Key
Vault Secrets User** role assignment, a renamed secret, a vault firewall — looks
identical to a working one in the portal while the container receives an empty
string. `<EMPTY>` versus `<UNSET>` names that failure directly.

Secret values are **never** rendered, flag or no flag: names matching
`KEY|SECRET|TOKEN|PASSWORD|CONNECTION_STRING|CREDENTIAL|SAS` show a length and a
short SHA-256 fingerprint instead, and secrets under 16 characters omit even the
fingerprint. The hash is there so you can tell "the key changed between
revisions" from "this matches the vault" without seeing either.

The list cannot drift from the code: it is recorded as a side effect of the
`_env()` helpers in [`config.py`](src/rag/config.py), so a new setting appears in
the report the moment it is read.

```powershell
# on, to diagnose
az containerapp update -g $ResourceGroup -n ca-rag-assistant `
    --set-env-vars SHOW_ENV_VALUES=true

# off again
az containerapp update -g $ResourceGroup -n ca-rag-assistant `
    --set-env-vars SHOW_ENV_VALUES=false
```

**Turning it off has to be explicit.** `--set-env-vars` *merges* — it never
removes — so there is no state in which the variable simply stops being `true`.
`--remove-env-vars SHOW_ENV_VALUES` is the other way, and differs subtly: it
deletes the variable and lets the code default (`false`) apply, rather than
pinning it. Either is fine; doing neither leaves the report exposed.

Each of these creates a **new revision**, so the change takes effect only once
that revision is serving traffic — check with
`az containerapp revision list -g $ResourceGroup -n ca-rag-assistant -o table`
before concluding the flag did not work.

> **The deploy commands fight you here, deliberately.** §6.5, §6.6 and §6.7 all
> assert `SHOW_ENV_VALUES=false`, so running any of them mid-investigation turns
> the report back off. That is the intended safety net, not a bug — re-assert
> `true` afterwards if you are still diagnosing.

> **Turn it off when you are done.** `/health` carries no auth dependency and
> ingress is public. Values are safe, but the variable *names* map your
> deployment and are worth not publishing.

---

## 8. Operations

**When documents change.** Re-run `python scripts/ingest.py`. It is incremental:
unchanged documents are neither parsed nor embedded, modified documents have
their previous chunks purged before new ones land, and deleted documents are
removed. Re-running with no changes makes zero embedding calls. Details in
[docs/ingestion-flow.md](docs/ingestion-flow.md).

**When the code changes.** Commit, rebuild, then point the app at the new image.
All three matter: an uncommitted change produces a tag that does not describe it,
building alone changes nothing that is running, and updating without `--image`
creates a revision that inherits the old one.

```powershell
# 1. commit -- the tag IS the commit, so this comes first
git commit -am "..."

# 2. recompute the preamble; $Sha now names the commit you just made
$Acr   = "acrragprodkb"
$Sha   = git rev-parse --short HEAD
$Image = "$Acr.azurecr.io/rag-assistant:$Sha"

# 3. build and push
az acr build -r $Acr -t rag-assistant:$Sha --build-arg BAKE_INDEX=false .

# 4. deploy it -- this is the step that actually changes the running code
az containerapp update -g $ResourceGroup -n ca-rag-assistant --image $Image

# 5. confirm the new revision is the one serving, and that it is running YOUR commit
az containerapp revision list -g $ResourceGroup -n ca-rag-assistant -o table
az containerapp show -g $ResourceGroup -n ca-rag-assistant `
  --query "properties.template.containers[0].image" -o tsv     # ends in $Sha

# 6. confirm it came up correctly
curl -s "https://<fqdn>/health" | ConvertFrom-Json | Select-Object -ExpandProperty providers
```

**Step 5 is not optional.** An update creates a revision; it does not promise the
revision is healthy or that traffic moved to it. A revision that fails to start
leaves the previous one serving — which presents as "the deploy did nothing".
Because the tag names a commit, that check is now conclusive: the image string
either ends in the commit you just built or it does not.

**Never rebuild an existing tag.** Committing first makes this automatic, which
is the point — a new commit is a new tag. Reusing one puts two revisions under a
single label over different code, with nothing afterwards able to say which is
which. It also fails mechanically: an update whose image string is unchanged,
with nothing else changed in the template, has no diff to apply and may produce
no new revision at all, so the build never rolls out.

**If you must build without committing** — a quick experiment — the tag will not
describe the image, and §6.1's preamble warns about it. Pin by digest instead,
which is derived from the content and cannot mislead:

```powershell
$Digest = az acr repository show -n $Acr --image rag-assistant:$Sha `
            --query digest -o tsv
$Pinned = "$Acr.azurecr.io/rag-assistant@$Digest"
```

Read back from the registry rather than parsed out of `az acr build`, whose
streamed logs share stdout with its result. Then deploy `$Pinned`:

```powershell
az containerapp update -g $ResourceGroup -n ca-rag-assistant --image $Pinned
```

And if a tag genuinely has to be reused, `az containerapp revision copy` forces a
fresh revision rather than relying on a template diff that is not there.

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

### Chasing a correlation id

When the UI shows

> Error: An unexpected error occurred. Quote the correlation id when reporting this.

the full traceback is **already recorded**, and no Application Insights setup is
needed to read it. The API's outermost middleware catches every unhandled
exception, logs it with `log.exception(...)`, and returns the same correlation id
it just logged under — so the id on screen is the join key. The JSON formatter
puts the traceback in an `exception` field and the id in `correlation_id` on
**every** line.

**Fastest path — tail the container's own log stream:**

```powershell
az containerapp logs show -g $ResourceGroup -n ca-rag-assistant --tail 200
az containerapp logs show -g $ResourceGroup -n ca-rag-assistant --follow   # live
```

`--tail` is capped at 300 lines and reads only the running replica, which is
enough while reproducing a fault but not for anything historical.

#### The full answer trail for one correlation id

Every question writes one `answer trail` line carrying what the request actually
did. **Application Insights has none of this** — the exporter is a stub, so the
route is Log Analytics over container stdout, and these queries are how you read
it.

```kusto
ContainerAppConsoleLogs_CL
| where ContainerAppName_s == "ca-rag-assistant"
| extend d = parse_json(Log_s)
| where tostring(d.correlation_id) == "<paste-the-id>"
| where tostring(d.message) == "answer trail"
| project TimeGenerated,
          question      = d.question,            // needs LOG_ANSWER_TRAIL
          standalone    = d.standalone_query,
          status        = d.status,
          confidence    = d.confidence,
          groundedness  = d.groundedness,
          stale_source  = d.grounded_in_superseded,
          rerank_method = d.rerank_method,       // azure-semantic | llm | lexical
          cited         = d.cited_docs,
          versioning    = d.versioning,
          total_ms      = d.total_ms,
          answer        = d.answer               // needs LOG_ANSWER_TRAIL
```

**Every retrieved chunk, every score, and its currency:**

```kusto
ContainerAppConsoleLogs_CL
| where ContainerAppName_s == "ca-rag-assistant"
| extend d = parse_json(Log_s)
| where tostring(d.correlation_id) == "<paste-the-id>"
| where tostring(d.message) == "answer trail"
| mv-expand hit = d.hits
| project doc_id     = tostring(hit.doc_id),
          section    = tostring(hit.section_path),
          department = tostring(hit.department),
          vector     = todouble(hit.vector_score),
          keyword    = todouble(hit.keyword_score),
          rrf        = todouble(hit.rrf_score),
          rerank     = todouble(hit.rerank_score),
          recency    = todouble(hit.recency_boost),
          final      = todouble(hit.score),
          current    = tobool(hit.is_current),
          version    = tostring(hit.version),
          snippet    = tostring(hit.snippet)     // needs LOG_ANSWER_TRAIL
| order by final desc
```

`rerank` null on every row answers **"was the reranker applied at all?"**;
`rerank_method` names which one ran. The scores are kept separate rather than
fused precisely so "which signal put this chunk on top?" has an answer.

#### Truthfulness is three questions, and only two are answerable here

| | Answerable at runtime | Field |
|---|---|---|
| **Groundedness** — is every claim supported by the retrieved context? | yes | `groundedness`, `groundedness_method`, `citations_valid` |
| **Source currency** — was the answer built on a *current* document? | yes | `grounded_in_superseded`, per-hit `is_current` / `version` |
| **Correctness** — is the answer actually *true*? | **no** | needs ground truth — `python eval/run_eval.py` |

The middle one is the trap groundedness alone will not catch: **a perfectly
grounded answer can still be false** if it faithfully quotes a superseded
document. `grounded_in_superseded` is scoped to the documents actually **cited**,
because retrieval routinely turns up an older version and demotes it — that is
the version ranking working, not a fault.

**Correctness, "did we retrieve the right document", and response completeness
all need labels** and belong to `eval/run_eval.py`, which scores 35 labelled
questions for hit@1, hit@5, MRR, document recall, citation precision and
groundedness. Runtime logging says what happened for *this* user; the eval
harness says whether the system is right in general. Keeping them apart is what
stops a confident number appearing with nothing behind it.

#### Alerting — log-search rules, not App Insights

```kusto
// answered from a superseded document, when the user did NOT ask for history.
// The wants_history guard matters: a question about 2025 SHOULD cite the 2025 doc.
ContainerAppConsoleLogs_CL
| extend d = parse_json(Log_s)
| where tostring(d.message) == "answer trail"
| where tobool(d.grounded_in_superseded)
| where not(tobool(d.versioning.wants_history))
| summarize stale = count() by bin(TimeGenerated, 1h)
```

```kusto
// grounding falling, or abstention climbing
ContainerAppConsoleLogs_CL
| extend d = parse_json(Log_s)
| where tostring(d.message) == "answer trail"
| summarize avg_groundedness = avg(todouble(d.groundedness)),
            abstained = countif(tostring(d.status) != "answered"),
            n = count()
        by bin(TimeGenerated, 15m)
| where avg_groundedness < 0.5 or todouble(abstained) / n > 0.3
```

```kusto
// the reranker or the embedder silently changed underneath you
ContainerAppConsoleLogs_CL
| extend d = parse_json(Log_s)
| where tostring(d.message) == "answer trail"
| summarize count() by bin(TimeGenerated, 1h),
            rerank = tostring(d.rerank_method),
            embedder = tostring(d.providers.embeddings)
```

That last one is worth wiring up first: it reports `embedder=local-hashing` the
moment a fallback happens, which is the failure this deployment spent a long time
diagnosing from a vector-length error three layers away.

**For history, or to search by id — Log Analytics.** Do not paste a table name
from memory: **which table holds these logs depends on how the environment was
configured**, and getting it wrong produces
`Failed to resolve table or column expression named …`. Find it in three steps.

**Step 1 — where are the logs actually going?**

```powershell
az containerapp env show -g $ResourceGroup -n cae-rag-prod `
    --query "properties.appLogsConfiguration" -o json
```

| `destination` | Table to query |
|---|---|
| `log-analytics` (the default) | `ContainerAppConsoleLogs_CL` — a *custom* table, note the suffix |
| `azure-monitor` | `ContainerAppConsoleLogs` — resource-specific, **no `_CL`** |
| `none` | Nothing is exported. No query will ever work; use `logs show` |

The same object carries `logAnalyticsConfiguration.customerId` — the workspace
GUID you query.

**Step 2 — query the workspace, *not* Application Insights.** This is the trap
that produces the error above with a perfectly correct table name. §6.2 also
creates `appi-rag-prod`, and Application Insights → Logs offers an identical KQL
window — but it is scoped to App Insights' own tables (`requests`, `traces`,
`exceptions`). Container logs are not visible there **even when they exist in the
workspace**. In the portal, open the **Log Analytics workspace → Logs**.

**Step 3 — find the workspace's *name*.** Step 1 gives its `customerId`, which is
a GUID; `table list` wants the resource name, and they are not the same thing.
You almost certainly never chose that name: §6.2 creates the environment without
`--logs-workspace-id`, so Azure auto-provisions a workspace called something like
`workspace-rgopenaiabcd`. List them:

```powershell
az monitor log-analytics workspace list `
    --query "[].{name:name, group:resourceGroup, customerId:customerId}" -o table
```

If the subscription has several, match the `customerId` column against the GUID
from step 1 — that is the one this environment writes to. Narrow with
`-g $ResourceGroup` if the list is long; the auto-created workspace usually lives
in the environment's own resource group.

**Then list the tables rather than guessing.** This subcommand is built in; no
extension needed:

```powershell
$Workspace = "<name-from-the-list-above>"

az monitor log-analytics workspace table list `
    -g $ResourceGroup --workspace-name $Workspace `
    --query "[].name" -o tsv | Where-Object { $_ -like "*ContainerApp*" }
```

Use the **workspace's own** resource group for `-g` if it differs from the
container app's.

> **Why the filtering is done in PowerShell rather than in JMESPath.** The
> obvious `--query "[?contains(name,'ContainerApp')].name"` **fails on Windows**
> with a message that does not look like it came from `az` at all:
>
> ```
> az : ].name was unexpected at this time.
> ```
>
> That is **cmd.exe**, not the CLI. `az` on Windows is `az.cmd`, a batch shim,
> and it strips the quotes before re-invoking Python — so cmd.exe ends up parsing
> the bare `(` and `)` as its own metacharacters. Adding quotes, single quotes or
> `^` escapes does not help, because the quoting is lost inside the shim.
>
> Keeping parentheses out of `--query` sidesteps it entirely, and the pipeline
> reads better anyway. When a JMESPath **function** is genuinely required, the one
> form that survives is a single-quoted string *containing* double quotes, so the
> inner pair reaches cmd.exe intact: `--query '"contains(@,''x'')"'`. Every other
> `--query` in this document is parenthesis-free and unaffected.

An **empty result** is itself the answer: no container logs have been ingested
yet. A custom table does not exist until its first record lands, and ingestion
lags a minute or two — so a query run straight after reproducing a fault can
legitimately find nothing. Use `az containerapp logs show` for anything you have
just triggered.

Then run the query, substituting the table name step 3 returned:

```kusto
// ContainerAppConsoleLogs_CL  when destination = log-analytics (default)
// ContainerAppConsoleLogs     when destination = azure-monitor
ContainerAppConsoleLogs_CL
| where ContainerAppName_s == "ca-rag-assistant"
| extend d = parse_json(Log_s)
| where tostring(d.correlation_id) == "<paste-the-id-from-the-UI>"
| project TimeGenerated, level = d.level, logger = d.logger,
          message = d.message, exception = d.exception
| order by TimeGenerated asc
```

That returns the request's whole story in order, ending in the traceback. To find
recent failures when you have no id to start from, swap the `where` for
`| where d.level == "ERROR"`.

**If the table resolves but a column does not**, the two tables do not share a
schema — `Log_s` and `ContainerAppName_s` are the custom table's columns. Run
`<table> | take 5` and read the real shape before trusting the projection above.

From the CLI, running KQL needs an extension that is **not** installed by default
(unlike `workspace table list`):

```powershell
az extension add -n log-analytics --upgrade
$WorkspaceId = az containerapp env show -g $ResourceGroup -n cae-rag-prod `
    --query "properties.appLogsConfiguration.logAnalyticsConfiguration.customerId" -o tsv
az monitor log-analytics query -w $WorkspaceId --analytics-query "<the KQL above>" -o table
```

**What the traceback usually says here.** A non-2xx from Azure AI Search becomes
a `RuntimeError` in [`store/azure_search.py`](src/rag/store/azure_search.py), and
the retrieval fan-out does not swallow it — so it surfaces as exactly this 500:

| In the traceback | Means |
|---|---|
| `-> 401` or `-> 403` | The search key is wrong, or was set but the revision never restarted. `az containerapp revision list -g $ResourceGroup -n ca-rag-assistant -o table` |
| `-> 404 … index … was not found` | Ingestion has not run against this service — §6.3 |
| Azure OpenAI 401 / `DeploymentNotFound` | `AZURE_OPENAI_API_KEY` missing (§6.6), or a deployment name that does not exist |

> **Application Insights is not wired up.**
> `APPLICATIONINSIGHTS_CONNECTION_STRING` is read into `Settings` and consumed by
> nothing — there is no exporter, and no OpenTelemetry dependency. The spans are
> *shaped* for it (one correlation id per request, one timed span per stage), but
> sending them anywhere means adding an exporter and a dependency to match, which
> would break this repo's deliberate no-`azure-*` rule. Everything above uses the
> log stream instead, which carries the same correlation id.

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
| `rerank_method` is `lexical` although semantic ranking **is** enabled on the service | A failed search put semantic into a back-off window. Only Azure's own "semantic … not enabled" wording triggers it; anything else now propagates instead. Historically **any** error disabled semantic permanently, so a vector-width mismatch could cost it until the revision restarted | Read `error=` on the `semantic ranking unavailable` log line — it carries Azure's actual message. Semantic retries itself after `SEMANTIC_RETRY_SECONDS` (15 min); `/health` → `index.semantic` shows it return |
| After a width mismatch, should the index be re-ingested? | **Usually no.** The index dimension is set by whichever embedder ran ingestion, and the fallback embedder is always 768 — so a 1536-wide index proves Azure embeddings built it. The mismatch is on the *query* side | Fix the embedder; the existing index starts matching again. `sync()` refuses a mismatched ingest anyway. Re-ingest only to change model or width deliberately — and since a vector field's dimensions are immutable, that means delete, recreate, re-ingest |
| KQL: `Failed to resolve table or column expression named 'ContainerAppConsoleLogs_CL'` | Usually the **scope**: you are in Application Insights → Logs, which cannot see container logs. Otherwise the table name differs by log destination (`_CL` only for `log-analytics`), or nothing has been ingested yet | Query the **Log Analytics workspace**, and list the tables first — §8 |
| `az : ].name was unexpected at this time.` (or `-o was unexpected…`) | **cmd.exe**, not `az`. `az.cmd` strips the quotes around `--query` before calling Python, so parentheses in a JMESPath expression are parsed as cmd metacharacters | Keep `(` `)` out of `--query` and filter with `Where-Object` instead; if a JMESPath function is unavoidable use `--query '"fn(@)"'` — §8 |
| `The index '<index>' for service '<service>' was not found` | The **service** was reached; the **index inside it** does not exist. These are different objects with different names — §5.2 | Re-run `python scripts/ingest.py --force`, which creates it. Do **not** set `AZURE_SEARCH_INDEX` to the service name |
| **Deployed app answers `insufficient_evidence` to everything, in every department** | `RETRIEVER_BACKEND=azure` but `AZURE_SEARCH_API_KEY` is unset, so the backend factory fell back to the **local** store — which is empty in a `BAKE_INDEX=false` image. It warns once at startup and then looks healthy | `GET /health` shows `"backend": "local"`, `"documents": 0`. Wire the key from Key Vault — §6.6 |
| Startup log says `falling back to the local backend` | Same as above: `has_azure_search` needs **both** `AZURE_SEARCH_ENDPOINT` and `AZURE_SEARCH_API_KEY`; one alone is silently insufficient | §6.6 |
| `/health` → **503**, `"status": "degraded"`, with a `startup_errors` block | **Deliberate.** With `RETRIEVER_BACKEND=azure` the index's vector width and the embedder's must match; the app refuses traffic rather than 500 on every question. `/chat` returns 503 quoting the same reason | `curl /health` and read `startup_errors[0].reason` and `.fix`, plus the `embedding_decision` trace beside it — the whole diagnosis, no logs needed |
| `startup_errors[0].check` = `index_embedder_width_agreement` | The embedder is producing a different width than the index holds — almost always because it fell back to `local-hashing` (768) against a 1536 index | `reason` names why it fell back. Wire the key (§6.6), fix the deployment name, or re-ingest at the embedder's width |
| `startup_errors[0].check` = `store_backend_agreement` | `RETRIEVER_BACKEND=azure` but `AZURE_SEARCH_ENDPOINT`/`_API_KEY` is empty, so the store fell back to the local one — empty in a `BAKE_INDEX=false` image | Set the variable named in `reason`. A `secretref` resolving to an empty string looks identical to a correct one in the portal — `SHOW_ENV_VALUES` distinguishes `<EMPTY>` from `<UNSET>` |
| `embedding_decision` step `probe` = `FAILED`, error `DeploymentNotFound` | **Deployment name ≠ model name.** `AZURE_OPENAI_EMBEDDING_DEPLOYMENT` must be the name *you gave the deployment*, not the model it runs. The trace prints the exact URL called, so the wrong name is visible in it | `az cognitiveservices account deployment list -g $ResourceGroup -n <aoai> --query "[].{deployment:name, model:properties.model.name}" -o table` — use the **deployment** column. Note `AZURE_OPENAI_CHAT_DEPLOYMENT` falls back to `AZURE_OPENAI_DEPLOYMENT_NAME`, but embeddings have **no such fallback**, which is why chat can work while embeddings silently do not |
| New code deployed, but the old behaviour persists | `az containerapp update --set-env-vars` **without** `--image` creates a revision that inherits the previous image — new config, old code | Pass `--image $Image` — §8. Then read the running image back: `az containerapp show … --query "properties.template.containers[0].image" -o tsv`. It ends in a commit id, so it either matches the commit you built or it does not |
| Unsure which build a revision is running | Historic: a hand-picked tag like `1.0.0` reused across builds cannot answer it | The tag is the commit. `git log --oneline` the id from the image string to see exactly what is deployed |
| Log stream: `Unable to open a connection to your app` **and** the app serves nothing | Log streaming attaches to a *running replica*. If the container is crash-looping there is nothing to attach to, and this message is the symptom, not a network problem | Check `az containerapp revision list -o table`. With the default `STARTUP_FAIL_MODE=unready` a contract failure no longer crashes the container, so this should only appear for a genuine crash. Read the reason from Log Analytics — `ContainerAppConsoleLogs_CL`, §8 |
| `400 InvalidRequestParameter … the provided vector has a length of '768'` | The app fell back to the **local 768-dim embedder** while the index expects the model's width (1536 for `text-embedding-3-small`). The embedder needs **all three** of endpoint / key / deployment — usually `AZURE_OPENAI_API_KEY` is the missing one | Wire the key — §6.6 — then confirm `providers.embeddings` on `/health` is no longer `local-hashing` |
| Startup log says `falling back to the local embedder` | Same cause; the log line's `missing=` field names exactly which setting is absent | §6.6 |
| `WARNING: No credential was provided to access Azure Container Registry` | The registry has admin access disabled (correctly), and no managed identity holds `AcrPull` | Grant `AcrPull` to a user-assigned identity **before** creating the app, and pass `--registry-identity` — §6.5. For an app that already exists, §6.6 |
| `az acr build`: `UnicodeEncodeError: 'charmap' codec can't encode characters` | **The build is not failing.** The CLI crashed *printing* the log — the traceback ends in `colorama` → `cp1252.py`, called from `_stream_logs`, which only runs once the build is already going. pip's progress bar uses `━` (U+2501); a cp437/cp1252 console cannot encode it. The ACR task continues server-side | `$env:PYTHONIOENCODING = "utf-8"` before the build (§6.1), or `--no-logs`. **Check before rebuilding** — `az acr task list-runs -r acrragprodkb -o table`; the image is usually already pushed. `PIP_PROGRESS_BAR=off` in the Dockerfile removes the character at source |
| `az acr build`: `not authorized to perform Microsoft.ContainerRegistry/registries/push/write` | The mirror image of the row above, and easy to confuse with it: this is **you** lacking `AcrPush`, not the app lacking `AcrPull`. `Contributor` on the resource group does not imply push rights on the registry | Grant yourself `AcrPush` (or Contributor/Owner) scoped to the registry — §2.6 |
| `error during connect: … the docker daemon is not running` | Docker Desktop is not started. Applies only to the local `docker build` route | Start Docker Desktop — or take the `az acr build` route in §6.1, which needs no local daemon |
| `WARNING: User identity … is already assigned to containerapp` | The same identity was passed to **both** `--registry-identity` and `--user-assigned`; the CLI adds it twice. Harmless in itself — `--registry-identity` already attaches it | Drop `--user-assigned` — §6.5. On 5.1 this warning alone aborts the command, so check whether the app was created before retrying |
| `az` command dies on a **warning** with `NativeCommandError` | Windows PowerShell 5.1 turns any native stderr write into a terminating error when `$ErrorActionPreference = "Stop"` | Check whether the command actually succeeded before retrying; then use §5.2's `Invoke-Az` or set `$ErrorActionPreference = "Continue"` — §5.2 |
| Container unreachable through ingress | uvicorn bound to loopback | `--host 0.0.0.0` (already in the image `CMD`) |
| Replicas restart-looping | `/health` used as a liveness probe | Use TCP for liveness — §6.7 |
| Deleted documents still answered | Manifest lost between ingest runs | Persist `RAG_DATA_DIR` — §6.4 |
| Anyone can query any department | `API_ALLOW_ANONYMOUS` left at `true` | Set `false` |
| `/health` returns an `env` block listing your configuration | `SHOW_ENV_VALUES` left at `true` after troubleshooting. `--set-env-vars` merges, so it survives every later update until something sets it back | `--set-env-vars SHOW_ENV_VALUES=false` — §7. Secret **values** were never exposed; the variable *names* were |

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
