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

### 2. Set your API key

```bash
export API_KEY=<your-llm-api-key>
```

One key, for whichever provider `MODEL_NAME` implies. A `.env` file is **not**
read on this path.

### 3. Compile

```bash
# a single repository
MODEL_NAME=claude-3-7-sonnet-20250219 \
  ./scripts/run_container.sh run https://github.com/taviso/ctypes.sh.git

# the whole 149-repo benchmark, 4 containers at a time
PARALLEL=4 MODEL_NAME=claude-3-7-sonnet-20250219 \
  ./scripts/run_container.sh run \
  --data data/sampled_repos_149_cleaned_higher_split_compilable.jsonl

# just a slice of it
./scripts/run_container.sh run --data data/sampled_repos_149_cleaned_higher_split_compilable.jsonl \
  --start 0 --end 20
```

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

## Options

Set as environment variables before `run`:

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
| `TAVILY_API_KEY` | unset | Enables the retriever's web-search tool |
| `OPENAI_KEY` | unset | Only for `RAG_RETRIEVAL=True` (embeddings) |

To drive a container directly, without the script:

```bash
docker run --rm -e REPO_URL=<url> -e API_KEY="$API_KEY" -e OUTPUT_MODE=dir \
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
- **`scripts/run_container.sh`**, **`.dockerignore`** — new.
