#!/bin/bash
#
# Run BuildBench entirely inside containers. The host needs only Docker and a
# POSIX shell -- no Python environment, no pyjoern, no JVM.
#
# Usage:
#   scripts/run_container.sh build
#       Build the base image and the worker image (offline, from the local base).
#
#   scripts/run_container.sh run <repo-url> [more urls...]
#   scripts/run_container.sh run --data <jsonl> [--start N] [--end N]
#       Compile the given repos, one container each.
#
# Configuration (environment variables):
#   API_KEY        LLM provider key                          (required for 'run')
#   MODEL_NAME     default: o3-mini
#   PARALLEL       concurrent containers, default: 2
#   RETRIEVAL      True|False, default: True
#   MAX_TURNS      default: 10
#   TIMEOUT_BASH   per-command timeout in seconds, default: 3600
#   CORES          workers for the validation step, default: 8
#   MEMORY         per-container memory cap, default: 6g
#   WORKER_IMAGE   default: buildbench_worker
#
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

BASE_IMAGE="${BASE_IMAGE:-docker_image_compilation}"
WORKER_IMAGE="${WORKER_IMAGE:-buildbench_worker}"
MODEL_NAME="${MODEL_NAME:-o3-mini}"
PARALLEL="${PARALLEL:-2}"
RETRIEVAL="${RETRIEVAL:-True}"
PERFECT_RETRIEVAL="${PERFECT_RETRIEVAL:-False}"
RAG_RETRIEVAL="${RAG_RETRIEVAL:-False}"
MAX_TURNS="${MAX_TURNS:-10}"
TIMEOUT_BASH="${TIMEOUT_BASH:-3600}"
AGENTS_NUMBER="${AGENTS_NUMBER:-2}"
REFINE_TIMES="${REFINE_TIMES:-3}"
CORES="${CORES:-8}"
MEMORY="${MEMORY:-6g}"

cmd_build() {
    echo ">>> Building base image ($BASE_IMAGE) -- this takes a while (Joern is ~1.8 GB)"
    docker build -t "$BASE_IMAGE" -f src/Dockerfile_compilation .

    echo ">>> Building worker image ($WORKER_IMAGE) from the local base"
    docker build --build-arg BASE_IMAGE="$BASE_IMAGE" \
        -t "$WORKER_IMAGE" -f src/Dockerfile_k8s .

    # Import everything the agents need. worker_main is deliberately excluded:
    # importing it resolves a repo URL and exits when none is configured.
    echo ">>> Verifying the image can import everything the agents need"
    docker run --rm --entrypoint /opt/venv/bin/python "$WORKER_IMAGE" -c "
import sys; sys.path.insert(0, '/app/src')
import agents, prompts, bash_executor, build_info_retrieval
import compilation, tools, default_values, validation_pipeline
print('imports OK')
"

    docker run --rm --entrypoint bash "$WORKER_IMAGE" -c \
        'gcc --version | head -1 && cmake --version | head -1 && java -version 2>&1 | head -1'
    echo ">>> Build OK"
}

# Turn the arguments into config/repos.json, which the worker indexes by JOB_INDEX.
build_repo_list() {
    mkdir -p config
    if [ "${1:-}" = "--data" ]; then
        local data_file="$2"; shift 2
        local start=0 end=-1
        while [ $# -gt 0 ]; do
            case "$1" in
                --start) start="$2"; shift 2 ;;
                --end)   end="$2";   shift 2 ;;
                *) echo "Unknown option: $1" >&2; exit 1 ;;
            esac
        done
        # stdlib only -- deliberately no dependency on a host Python env
        python3 - "$data_file" "$start" "$end" <<'PY'
import json, sys
path, start, end = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
repos = [json.loads(line)["full_name"] for line in open(path) if line.strip()]
repos = repos[start:] if end == -1 else repos[start:end]
json.dump([f"https://github.com/{r}.git" for r in repos], open("config/repos.json", "w"), indent=2)
print(f"Wrote {len(repos)} repos to config/repos.json")
PY
    else
        python3 - "$@" <<'PY'
import json, sys
json.dump(list(sys.argv[1:]), open("config/repos.json", "w"), indent=2)
print(f"Wrote {len(sys.argv) - 1} repos to config/repos.json")
PY
    fi
}

cmd_run() {
    if [ -z "${API_KEY:-}" ]; then
        echo "ERROR: API_KEY is not set." >&2
        exit 1
    fi
    if [ $# -eq 0 ]; then
        echo "ERROR: give a repo URL or --data <jsonl>." >&2
        exit 1
    fi

    build_repo_list "$@"
    local count
    count=$(python3 -c 'import json; print(len(json.load(open("config/repos.json"))))')

    mkdir -p cloned_repos compiled_repos compiled_results all_logs autogen_logs

    echo ">>> Compiling $count repo(s), $PARALLEL at a time, image=$WORKER_IMAGE model=$MODEL_NAME"

    # One container per repo. --rm keeps stopped containers from piling up;
    # everything worth keeping is on a bind mount by then.
    seq 0 $((count - 1)) | xargs -P "$PARALLEL" -I{} \
        docker run --rm \
            --name "buildbench-{}" \
            --memory "$MEMORY" \
            -e JOB_INDEX={} \
            -e API_KEY="$API_KEY" \
            -e MODEL_NAME="$MODEL_NAME" \
            -e RETRIEVAL="$RETRIEVAL" \
            -e PERFECT_RETRIEVAL="$PERFECT_RETRIEVAL" \
            -e RAG_RETRIEVAL="$RAG_RETRIEVAL" \
            -e MAX_TURNS="$MAX_TURNS" \
            -e TIMEOUT_BASH="$TIMEOUT_BASH" \
            -e AGENTS_NUMBER="$AGENTS_NUMBER" \
            -e REFINE_TIMES="$REFINE_TIMES" \
            -e CORES="$CORES" \
            -e TAVILY_API_KEY="${TAVILY_API_KEY:-}" \
            -e OPENAI_KEY="${OPENAI_KEY:-}" \
            -e DEEPSEEK_BASE_URL="${DEEPSEEK_BASE_URL:-}" \
            -e MODEL_PRICE="${MODEL_PRICE:-}" \
            -e OUTPUT_MODE=dir \
            -v "$PROJECT_DIR/config:/app/config:ro" \
            -v "$PROJECT_DIR/cloned_repos:/app/cloned_repos" \
            -v "$PROJECT_DIR/compiled_repos:/app/compiled_repos" \
            -v "$PROJECT_DIR/compiled_results:/app/compiled_results" \
            -v "$PROJECT_DIR/all_logs:/app/all_logs" \
            -v "$PROJECT_DIR/autogen_logs:/app/autogen_logs" \
            "$WORKER_IMAGE" \
        || echo ">>> One or more repos failed; see all_logs/ and compiled_results/results.json"

    echo ">>> Done. Artifacts in compiled_repos/, metrics in compiled_results/results.json"
}

usage() {
    cat <<'EOF'
Run BuildBench entirely inside containers. The host needs only Docker.

Usage:
  scripts/run_container.sh build
  scripts/run_container.sh run <repo-url> [more urls...]
  scripts/run_container.sh run --data <jsonl> [--start N] [--end N]

Settings go BEFORE the script name, as environment variables:

  API_KEY=<key> MODEL_NAME=gemini-2.5-flash \
      scripts/run_container.sh run https://github.com/user/repo.git

  API_KEY        LLM provider key                       (required for 'run')
  MODEL_NAME     default: o3-mini. Routed by substring: gpt/o3/o4 -> OpenAI,
                 claude -> Anthropic, gemini -> Google, deepseek -> DeepSeek,
                 qwen -> HuggingFace router. Any model id the provider accepts
                 works; this path does not restrict the name.
  DEEPSEEK_BASE_URL  default: https://api.deepseek.com/v1
  MODEL_PRICE    "<prompt>,<completion>" per 1K tokens. AutoGen only knows
                 prices for OpenAI/Anthropic ids; without this it warns
                 "Model X is not found. The cost will be 0" and reports 0.
  PARALLEL       concurrent containers, default: 2
  MEMORY         per-container memory cap, default: 6g
  RETRIEVAL      True|False, default: True
  MAX_TURNS      default: 10
  TIMEOUT_BASH   per-command timeout in seconds, default: 3600
  CORES          workers for the validation step, default: 8
  WORKER_IMAGE   default: buildbench_worker
EOF
}

case "${1:-}" in
    build) shift; cmd_build "$@" ;;
    run)   shift; cmd_run   "$@" ;;
    -h|--help|help) usage ;;
    "")
        echo "ERROR: no command given; expected 'build' or 'run'." >&2
        echo >&2
        usage >&2
        exit 1
        ;;
    *)
        echo "ERROR: unknown command '$1'; expected 'build' or 'run'." >&2
        case "$1" in
            *=*) echo "       '$1' looks like a setting: put it before the script name, e.g." >&2
                 echo "       $1 scripts/run_container.sh run <repo-url>" >&2 ;;
        esac
        echo >&2
        usage >&2
        exit 1
        ;;
esac
