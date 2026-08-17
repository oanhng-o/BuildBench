# BuildBench: LLM-Assisted Compilation Benchmark

BuildBench is a benchmark and multi-agent system for automatically compiling C/C++ GitHub repositories using Large Language Models. Agents iteratively generate and execute shell commands, diagnosing and fixing build errors through dialogue. You can find the paper at [arXiv:2509.25248](https://arxiv.org/abs/2509.25248).

## Setup

### Prerequisites

- Python 3.10+
- Docker
- (Optional) Kubernetes cluster for batch execution

### Installation

```bash
conda create -n BuildBench python=3.10
conda activate BuildBench
pip install -r requirements.txt
```

### Docker Images

For the host-driven modes below. The container-only path builds its own images —
skip this and see [Container-Only](#container-only-no-host-python-environment).

```bash
# Build base image (compilers, build tools, Python deps)
sudo docker build -t docker_image_compilation -f src/Dockerfile_compilation .

# Build K8S worker image
sudo docker build -t docker_image_compilation_k8s -f src/Dockerfile_k8s .
```

## Running

### Container-Only (no host Python environment)

Clone, compile and validate all happen inside one container — the host needs only
Docker. See [CONTAINER_ONLY.md](CONTAINER_ONLY.md).

```bash
cp .env.example .env     # then fill in API_KEY
./scripts/run_container.sh run https://github.com/user/repo.git
```

The images are built on first use, so that is all a fresh clone needs. Run
`./scripts/run_container.sh build` first if you would rather get the ~10 min
base build out of the way separately.

The modes below instead drive containers from the host, which requires the
`requirements.txt` environment (including `pyjoern` and a JVM for validation).

### Single Repository (Local Docker)

```bash
python3 src/main.py --api_key=$API_KEY --model_name=o3-mini \
  --github_repo=https://github.com/user/repo.git --random=-1 \
  --host_project_dir=$(pwd) --docker_image=docker_image_compilation
```

Note the explicit `--docker_image`: the default is the published K8S image, whose
`ENTRYPOINT` makes it exit immediately when driven by `src/main.py`.

### All Repositories (Local Docker)

```bash
python3 src/main.py --api_key=$API_KEY --model_name=o3-mini \
  --host_project_dir=$(pwd)
```

### Kubernetes Batch Execution

```bash
python3 orchestrator.py --api_key=$API_KEY --model_name=claude-3-7-sonnet-20250219 \
  --docker_image=your_registry/llm_assisted_compilation_k8s:latest \
  --k8s_parallelism=50 --data_path=data/sampled_repos_149_cleaned_higher_split_compilable.jsonl \
  --retrieval --host_project_dir=$(pwd)
```

### Tests

```bash
python3 tests/API_key_test.py
python3 tests/docker_test.py
python3 tests/get_repo_test.py
python3 tests/link_extraction_test.py
```

## Architecture

### Execution Flow

```
orchestrator.py → K8S Jobs → worker_main.py → compilation.py → Agent.compile_repo()
                                                                   ↓
src/main.py (local) ──────→ compilation.py → Agent.compile_repo()
```

1. **Entry points**: `orchestrator.py` (K8S batch), `src/main.py` (local Docker), `src/compilation.py` (inside container)
2. **Agent dialogue**: `src/agents.py` — creates a GroupChat with 2–3 AutoGen agents (Bash Command Generator + Executor, optionally a Build Instructions Retriever). Agents loop: generate commands → execute → analyze errors → retry.
3. **Compilation terminates** when agents output "terminate" keyword or hit `max_turns`.

### Key Components

| File | Description |
|------|-------------|
| `src/agents.py` | Multi-agent orchestration using Microsoft AutoGen |
| `src/prompts.py` | Agent system prompts with build rules |
| `src/bash_executor.py` | Safe command executor with sanitization |
| `src/build_info_retrieval.py` | Heuristic retrieval from README/docs |
| `src/RAG_retrieval.py` | LangChain/Chroma vector search over docs |
| `src/validation_pipeline.py` | Compilation validation (source vs binary functions) |
| `src/worker_main.py` | K8S pod entry point |

### Retrieval Modes

Three retrieval strategies supply build context to agents:

1. **Perfect Retrieval** (`--perfect_retrieval`): Hand-curated URLs from `PERFECT_RETRIEVAL_DICT`
2. **RAG Retrieval** (`--RAG_retrieval`): Vector search over repo docs using LangChain/Chroma
3. **Heuristic Retrieval** (`--retrieval`): LLM-driven extraction from README and build files

### Supported LLM Providers

- **OpenAI** (GPT-4o, O3-mini): standard OpenAI API
- **Claude** (Claude 3.5/3.7 Sonnet): `api_type="anthropic"`
- **Gemini**: Google Generative AI API
- **Qwen**: via HuggingFace router

## Baselines

The `src/compilation_baselines/` directory contains all baseline build methods evaluated in Table 1:

| Baseline | Type | Description |
|----------|------|-------------|
| **GHCC** | Rule-based | Heuristic Makefile-based compilation ([huzecong/ghcc](https://github.com/huzecong/ghcc)) |
| **Assemblage** | Rule-based | Automated build system detection ([Assemblage-Dataset/Assemblage](https://github.com/Assemblage-Dataset/Assemblage)) |
| **CompileAgent** | Multi-Agent | Flow-based agent with CompileNavigator + ErrorSolver ([Hu et al., 2025](https://arxiv.org/abs/2505.04254)) |
| **LLM Baseline** | Single-Turn | One-shot LLM command generation (o3-mini, Claude 3.7-Sonnet) |
| **LLM Multi-Turn** | Multi-Turn | Iterative LLM with error refinement |

```bash
# Run a baseline
python src/compilation_baselines/main.py --compilation_method assemblage \
  --input_file_path data/sampled_repos_149_cleaned_higher_split_compilable.jsonl
```

See `src/compilation_baselines/README.md` for detailed baseline documentation.

## Project Structure

```
├── src/                    # Core compilation system
│   ├── compilation_baselines/  # Baseline build methods (GHCC, Assemblage, CompileAgent, LLM)
│   └── ...                 # OSS-BUILD-AGENT components
├── postprocessing/         # Result analysis pipeline
├── data_labeling/          # Human evaluation Dockerfiles
├── data/                   # Benchmark data & references
├── tests/                  # Test suite
├── scripts/                # Build & deployment scripts
├── orchestrator.py         # K8S batch orchestration
└── job-template.yaml       # K8S job template
```

See `postprocessing/README.md`, `data_labeling/README.md`, `data/README.md`, and `src/compilation_baselines/README.md` for component-specific documentation.

### Evaluating Results

After compilation, evaluate Strict/Flexible success metrics against expert-labeled binary names:

```bash
python3 postprocessing/evaluate_success.py \
  --ground_truth data/compilation_label.json \
  --compiled_dir compiled_repos/ \
  --output compiled_results/evaluation.json
```

Run postprocessing analysis on compiled binaries:

```bash
python3 postprocessing/postprocessing.py --compiled_dir compiled_repos/ --results_dir compiled_results/
```

## Data

- **Validation Set (70 repos)**: Included in `data/Compilation_Bench_Validation_Set_70.csv`
- **Full Benchmark (385 repos)** and **Compilable Subset (149 repos)**: Available on [HuggingFace](https://huggingface.co/datasets/STEVENZHANG904/Build_Bench_Validation_Data)

See `data/README.md` for schema details and download instructions.

## Environment Variables

| Variable | Description |
|----------|-------------|
| `API_KEY` | LLM provider API key |
| `MODEL_NAME` | Model to use (e.g., `o3-mini`, `claude-3-7-sonnet-20250219`) |
| `TIMEOUT_BASH` | Command timeout in seconds |
| `MAX_TURNS` | Maximum agent conversation turns |
| `CORES` | Number of CPU cores for parallel compilation |
| `RETRIEVAL` | Enable heuristic retrieval (`True`/`False`) |
| `RAG_RETRIEVAL` | Enable RAG retrieval (`True`/`False`) |
| `PERFECT_RETRIEVAL` | Enable perfect retrieval (`True`/`False`) |
| `TAVILY_API_KEY` | (Optional) Tavily API key for web search |
| `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_BASE_URL` | (Optional) Trace every build to Langfuse — see [Tracing](#tracing-langfuse) |
| `BUILDBENCH_RUN_ID` | (Optional) Groups a run's repos into one Langfuse session; defaults to a timestamp |

## Tracing (Langfuse)

Set the three `LANGFUSE_*` variables and each repository build is traced: one
trace per repo, containing the agent conversation, every LLM call with its
prompt, completion and token usage, and every bash command with its exit code.
When validation finishes, `compiled_percentage` and `is_compiled` are attached
to the same trace as scores, so behaviour and outcome sit together.

```bash
LANGFUSE_PUBLIC_KEY=pk-lf-... LANGFUSE_SECRET_KEY=sk-lf-... \
LANGFUSE_BASE_URL=https://cloud.langfuse.com \
  ./scripts/run_container.sh run https://github.com/user/repo.git
```

Leave the keys unset, or set `LANGFUSE_TRACING=False`, and tracing is off. It is
best-effort in any case: an unreachable Langfuse never fails a build. See
[CONTAINER_ONLY.md](CONTAINER_ONLY.md#tracing-langfuse) for the trace layout and
its caveats.

## Citation

```bibtex
@article{zhang2025buildbench,
  title={BuildBench: Benchmarking LLM Agents on Compiling Real-World Open-Source Software},
  author={Zhang, Zehua and Bajaj, Ati Priya and Handa, Divij and Liu, Siyu and Raj, Arvind S and Chen, Hongkai and Wang, Hulin and Liu, Yibo and Basque, Zion Leonahenahe and Nath, Souradip and Juneja, Vishal and Chapre, Nikhil and Bao, Tiffany and Shoshitaishvili, Yan and Doup{\'e}, Adam and Baral, Chitta and Wang, Ruoyu},
  journal={arXiv preprint arXiv:2509.25248},
  year={2025}
}
```

## License

MIT License — see [LICENSE](LICENSE) for details.
