# Running BuildBench container-only

Clone, compile and validate all happen inside one container. The host needs
**only Docker** — no conda, no Python environment, no Joern download, no JVM.

(`src/main.py` is the other way to run this: it drives containers *from* the
host, so the host needs the full `requirements.txt` environment. That path still
works and is untouched.)

## Steps

### 1. Build the images

```bash
./scripts/run_container.sh build
```

~10 min and ~9.5 GB: Ubuntu 22.04 with gcc/cmake/ninja/Java 17, plus Joern
(1.7 GB) and Playwright browsers. Ends by printing `Build OK`.

### 2. Configure

```bash
cp .env.example .env
```

Then fill in `API_KEY` — one key, for whichever provider `MODEL_NAME` implies.
`.env` is gitignored and never enters an image.

```ini
API_KEY=sk-...
MODEL_NAME=deepseek-chat
```

### 3. Compile

```bash
# a single repository
./scripts/run_container.sh run https://github.com/taviso/ctypes.sh.git

# the whole 149-repo benchmark
./scripts/run_container.sh run \
  --data data/sampled_repos_149_cleaned_higher_split_compilable.jsonl

# just a slice of it
./scripts/run_container.sh run --data data/sampled_repos_149_cleaned_higher_split_compilable.jsonl \
  --start 0 --end 20
```

Everything else is read from `.env`, so the command line carries only the repos.
For a one-off change, put the setting *before* the script name — see
[Changing a value](#changing-a-value).

### 4. Read the results

| Path | Contents |
|---|---|
| `compiled_repos/<repo>/` | Build artifacts |
| `compiled_results/results.json` | Compiled percentage, function counts, timings |
| `all_logs/<repo>/` | Per-repo log |
| `autogen_logs/<repo>/` | Agent conversation transcript |

### 5. Evaluate (optional)

```bash
docker run --rm -v "$(pwd):/work" -w /work \
  --entrypoint /opt/venv/bin/python buildbench_worker \
  postprocessing/evaluate_success.py \
    --ground_truth data/compilation_label.json \
    --compiled_dir compiled_repos/ --output compiled_results/evaluation.json
```

## Changing a value

**Edit `.env`, then just run again. No rebuild** — settings are mounted at start
time, not baked into the image.

If the change seems ignored, an exported shell variable is shadowing it —
environment beats `.env`:

```bash
env | grep -E 'API_KEY|MODEL_NAME'   # anything here wins over .env
unset API_KEY MODEL_NAME             # .env is authoritative again
```

Same rule gives you a one-off override, and `ENV_FILE` a second config:

```bash
MODEL_NAME=gemini-2.5-flash ./scripts/run_container.sh run <repo-url>
ENV_FILE=configs/deepseek.env ./scripts/run_container.sh run <repo-url>
```

## Options

Set in `.env`, or on the command line before `run`:

| Variable | Default | Meaning |
|---|---|---|
| `API_KEY` | — | **Required** |
| `MODEL_NAME` | `o3-mini` | Provider is routed by substring: `gpt`/`o3`/`o4`, `claude`, `gemini`, `deepseek`, `qwen`. Any id the provider accepts works — this path does not restrict the name (`src/tools.py` `choices=` only constrains `src/main.py`) |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com/v1` | Override for a proxy or self-hosted deployment |
| `PARALLEL` | `2` | Concurrent containers |
| `MEMORY` | `6g` | Per-container memory cap |
| `RETRIEVAL` | `True` | Heuristic build-instruction retrieval |
| `MAX_TURNS` | `10` | Agent conversation turns |
| `TIMEOUT_BASH` | `3600` | Per-command timeout, seconds |
| `CORES` | `8` | Workers for the validation step |
| `MODEL_PRICE` | unset | `"<prompt>,<completion>"` per 1K tokens. AutoGen prices a run from its own table, which covers only OpenAI/Anthropic ids; without this a DeepSeek or Qwen run reports a cost of 0 |
| `TAVILY_API_KEY` | unset | Enables the retriever's web-search tool |
| `OPENAI_KEY` | unset | Only for `RAG_RETRIEVAL=True` (embeddings) |
| `LANGFUSE_SECRET_KEY`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_BASE_URL` | unset | Set all three and every build is traced to Langfuse — see [Tracing](#tracing-langfuse). Leave them unset and tracing is off |
| `LANGFUSE_TRACING` | `True` | Set to `False` to keep the keys but switch tracing off |
| `LANGFUSE_ENVIRONMENT` | `default` | Langfuse environment label, e.g. `staging` |
| `BUILDBENCH_RUN_ID` | timestamp | Groups one invocation's repos into a Langfuse session, and seeds the per-repo trace ids |
| `ENV_FILE` | `.env` | Which settings file to use |

## How the settings reach the container

Keys are **not** passed with `docker run -e`. The script writes the effective
config to a private file (mode 600, deleted on exit) and mounts it read-only at
`/app/.env`; `src/env_config.py` reads it there without touching `os.environ`.
So the key stays out of `docker inspect` *and* out of the environment of the
build scripts the agent runs. Lookup order: **file → environment → default** —
the fallback is what keeps the Kubernetes path working. Each run logs its
source: `config: /app/.env (6 keys) with environment fallback`.

Only non-secret knobs still travel as `-e`: `JOB_INDEX`, `RETRIEVAL`,
`PERFECT_RETRIEVAL`, `RAG_RETRIEVAL`, `MAX_TURNS`, `TIMEOUT_BASH`,
`AGENTS_NUMBER`, `REFINE_TIMES`, `CORES`, `OUTPUT_MODE`, `BUILDBENCH_RUN_ID`.

## Tracing (Langfuse)

Fill the three `LANGFUSE_*` keys in `.env` and each build writes one trace:

```
redis                              <- one trace per repo, scored below
  retrieval-chat                   <- only in the 3-agent setup
    OpenAI-generation              <- prompt, completion, tokens, latency
    bash                           <- the command, its exit code, its output
  compilation-chat
    OpenAI-generation
    bash
    ...
```

All repos of one invocation share a **session** (`BUILDBENCH_RUN_ID`), so a run
reads as one list. Traces are tagged with the model, the agent count and the
retrieval mode. When validation finishes, its result is attached to the same
trace as the scores `compiled_percentage` and `is_compiled` — the conversation
and what it achieved end up in one place.

Notes:

- The trace id is derived from `(BUILDBENCH_RUN_ID, repo name)`, which is how
  the worker scores a trace written by the `compilation.py` subprocess without
  passing an id between the two.
- LLM calls are captured by patching the OpenAI SDK (`langfuse.openai`), which
  AutoGen uses for OpenAI, DeepSeek, Gemini and Qwen. Claude goes through the
  anthropic SDK, covered by `opentelemetry-instrumentation-anthropic`.
- AutoGen caches LLM responses (`cache_seed=41` in `src/agents.py`). A cache hit
  makes no API call, so it produces no generation in the trace.
- Cost has two independent sources, and a model unknown to either reports 0.
  `MODEL_PRICE` (USD per 1K tokens) fixes AutoGen's own accounting; Langfuse
  needs a model definition in project settings, matched by regex against the
  model id. Langfuse prices *per usage type*, so a reasoning model needs
  `output_reasoning_tokens` priced as well as `output`, and
  `input_cached_tokens` at the cache-hit rate — pricing only `input`/`output`
  silently undercounts. Cost is computed at ingestion: adding a definition does
  not repair traces already written.
- Tracing never fails a build: missing keys, a missing package, or an
  unreachable Langfuse turn it off and the run continues.

To drive a container directly, without the script:

```bash
docker run --rm -e REPO_URL=<url> -e OUTPUT_MODE=dir \
  -e BUILDBENCH_ENV_FILE=/app/.env -v "$(pwd)/.env:/app/.env:ro" \
  -v "$(pwd)/compiled_repos:/app/compiled_repos" \
  -v "$(pwd)/compiled_results:/app/compiled_results" \
  buildbench_worker
```

`REPO_URL` and `JOB_INDEX` are two entrances to the same worker: Kubernetes
indexes a ConfigMap with `JOB_INDEX`, a one-off `docker run` uses `REPO_URL`.
Other knobs: `REPOS_CONFIG`, `BUILD_DIR`, `OUTPUT_MODE` (`tarball`|`dir`|`both`).

## Changes relative to upstream

- **`src/Dockerfile_compilation`** — the `pyjoern` layer invoked
  `/opt/venv/bin/playwright` before any layer installed it, so the build died
  with exit 127 after downloading 1.7 GB of Joern. Fixed, plus added
  `python-dotenv` and `nest_asyncio` that the code imports.
- **`src/validation_pipeline.py`** — two fixes for repos that fail to build.
  `extract_binary_functions` asserted that artifacts exist, so a failed build
  crashed the worker instead of scoring 0%; it now returns an empty result. And
  the "no source functions" / "no binary functions" branches returned 2 values
  while both callers unpack 6, so the empty case raised `ValueError` that the
  callers' broad `except` swallowed and reported as a build failure. All
  branches return 6 now.
- **`src/tools.py`** — `tar` extraction gained `--no-same-owner`. Restoring
  uid/gid 0 fails on a VM-backed bind mount (Docker Desktop) and on NFS with
  root_squash, so `tar` exited 2 and the build output never reached the host.
- **`src/worker_main.py`** — accepts `REPO_URL` as an alternative to
  `JOB_INDEX` + ConfigMap; paths and output mode configurable; `results.json`
  writes serialized with `flock` so `PARALLEL > 1` no longer drops results.
- **`src/Dockerfile_k8s`** — `ARG BASE_IMAGE`, so the worker image builds from a
  local base instead of pulling `buildbench/compilation_base_image:17`.
- **`src/agents.py`**, **`src/build_info_retrieval.py`** — added DeepSeek
  (OpenAI-compatible endpoint, JSON mode for structured output). Also removed two
  hardcoded model ids in `llm_response_structured`: the Gemini branch always
  called `gemini-2.5-flash` and the Claude branch always called
  `claude-3-7-sonnet-20250219`, so retrieval ignored `MODEL_NAME` and 404'd once
  a provider retired that model. Every branch now uses the model passed in.
- **`src/env_config.py`** — new. Single place that resolves configuration, file
  first and environment second. Replaces the two `load_dotenv()` calls in
  `src/build_info_retrieval.py` and `src/RAG_retrieval.py`, which were no-ops in
  the container: `.env` is in `.dockerignore`, so there was no file to find.
- **`src/langfuse_tracing.py`** — new. Langfuse tracing for the agents, wired
  into `src/compilation.py` (one trace per repo), `src/agents.py` (a span per
  chat) and `src/bash_executor.py` (a span per command); scored from
  `src/worker_main.py` and `src/main.py`. Credentials are passed to the SDK
  constructor rather than exported, so they stay out of the environment the
  agent's build subprocesses inherit.
- **`scripts/run_container.sh`**, **`.env.example`**, **`.dockerignore`** — new.
  The script parses `.env` line by line rather than `source`-ing it, so a stray
  backtick in a pasted key cannot execute.
