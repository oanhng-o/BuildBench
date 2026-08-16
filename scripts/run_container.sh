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
# Configuration comes from .env in the project root (copy .env.example) or from
# the environment; the environment wins. Point ENV_FILE elsewhere to use a
# different file.
#
# .env is bind-mounted read-only into each container at /app/.env and read there
# by src/env_config.py. Keys are deliberately NOT passed with `docker run -e`, so
# they show up neither in `docker inspect` nor in the environment of the build
# scripts the agent runs inside the container.
#
#   API_KEY        LLM provider key                          (required for 'run')
#   MODEL_NAME     default: o3-mini
#   LANGFUSE_SECRET_KEY / LANGFUSE_PUBLIC_KEY / LANGFUSE_BASE_URL
#                  set them and every build is traced; unset, tracing is off
#   BUILDBENCH_RUN_ID  Langfuse session for this invocation, default: a timestamp
#   PARALLEL       concurrent containers, default: 2
#   RETRIEVAL      True|False, default: True
#   MAX_TURNS      default: 10
#   TIMEOUT_BASH   per-command timeout in seconds, default: 3600
#   CORES          workers for the validation step, default: 8
#   MEMORY         per-container memory cap, default: 6g
#   WORKER_IMAGE   default: buildbench_worker
#
set -euo pipefail

INVOKED_FROM="$PWD"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

# Read KEY=VALUE lines from .env into the environment. Deliberately not `source`:
# that would execute the file, and a stray backtick or $(...) in a pasted API key
# would run as a command. Variables already set in the environment are left
# alone, so `MODEL_NAME=x scripts/run_container.sh ...` still overrides the file.
load_env_file() {
    local file="$1" line key val
    [ -f "$file" ] || return 0

    while IFS= read -r line || [ -n "$line" ]; do
        line="${line%$'\r'}"                     # tolerate CRLF
        line="${line#"${line%%[![:space:]]*}"}"  # strip leading whitespace
        case "$line" in ''|'#'*) continue ;; esac
        line="${line#export }"

        key="${line%%=*}"
        [ "$key" = "$line" ] && continue         # no '=' on the line
        val="${line#*=}"

        key="${key%"${key##*[![:space:]]}"}"     # strip trailing whitespace
        case "$key" in ''|[0-9]*|*[!A-Za-z0-9_]*) continue ;; esac

        val="${val#"${val%%[![:space:]]*}"}"     # trim whitespace around the
        val="${val%"${val##*[![:space:]]}"}"     # value, then unquote, so that
        case "$val" in                           # "  x  " keeps its spaces
            \"*\") val="${val#\"}"; val="${val%\"}" ;;
            \'*\') val="${val#\'}"; val="${val%\'}" ;;
        esac

        [ -n "${!key+set}" ] && continue         # environment wins
        export "$key=$val"
    done < "$file"
}

ENV_FILE="${ENV_FILE:-$PROJECT_DIR/.env}"
# The file is bind-mounted into the container, and `docker -v` needs an absolute
# source -- a relative one would be read as a named volume. Resolve against the
# directory the user invoked from, not PROJECT_DIR, which we have already cd'd to.
case "$ENV_FILE" in
    /*) ;;
    *)  ENV_FILE="$INVOKED_FROM/$ENV_FILE" ;;
esac

# Host-side settings (PARALLEL, MEMORY, WORKER_IMAGE, ...) are read here. The
# container gets the same file on a read-only mount and reads it itself, so keys
# never travel through `docker run -e` -- see src/env_config.py.
load_env_file "$ENV_FILE"

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
# Ties every container of this invocation to one Langfuse session, and seeds the
# per-repo trace ids. It must differ between runs, or two runs of the same repo
# would write into the same trace.
BUILDBENCH_RUN_ID="${BUILDBENCH_RUN_ID:-$(date +%Y-%m-%d_%H-%M-%S)}"

# Keys the worker reads out of the mounted file. Host-only settings (PARALLEL,
# MEMORY, WORKER_IMAGE, ...) are deliberately absent: the container has no use
# for them, and a mount should carry the least it can.
CONTAINER_KEYS=(
    API_KEY
    MODEL_NAME
    MODEL_PRICE
    DEEPSEEK_BASE_URL
    LANGFUSE_SECRET_KEY
    LANGFUSE_PUBLIC_KEY
    LANGFUSE_BASE_URL
    LANGFUSE_TRACING
    LANGFUSE_ENVIRONMENT
    TAVILY_API_KEY
    OPENAI_KEY
    SUDO_PASSWORD
)

MOUNTED_ENV=""
cleanup() { [ -n "$MOUNTED_ENV" ] && rm -f "$MOUNTED_ENV"; }
trap cleanup EXIT INT TERM

# Write the effective configuration -- .env with any command-line override
# applied on top -- to a private file, and mount that instead of .env itself.
# Generating it is what keeps `MODEL_NAME=x scripts/run_container.sh run ...`
# working now that the worker reads a file rather than its environment.
write_mounted_env() {
    # Inside the project directory rather than /tmp: Docker Desktop bind-mounts
    # only paths on its File Sharing list, and /tmp is not on it by default --
    # the mount fails with "path ... is not shared from the host". The project
    # directory is already the source of every other mount below, so it is
    # necessarily shared.
    MOUNTED_ENV="$(mktemp "$PROJECT_DIR/.buildbench-env.XXXXXX")"
    chmod 600 "$MOUNTED_ENV"

    local key value
    for key in "${CONTAINER_KEYS[@]}"; do
        value="${!key:-}"
        [ -n "$value" ] || continue
        # A newline would end the KEY=VALUE line and turn the remainder into a
        # bogus entry. No credential format uses one; say so rather than corrupt
        # the file silently.
        case "$value" in
            *$'\n'*) echo "WARNING: $key contains a newline; skipping it." >&2; continue ;;
        esac
        printf '%s=%s\n' "$key" "$value" >> "$MOUNTED_ENV"
    done
}

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
    # Checked before the mount: docker silently creates a *directory* at a bind
    # source that does not exist, which would then shadow /app/.env with an empty
    # dir and leave the worker with no configuration at all.
    if [ ! -f "$ENV_FILE" ]; then
        echo "ERROR: settings file $ENV_FILE does not exist." >&2
        echo "       Run 'cp .env.example .env' and fill in API_KEY=" >&2
        exit 1
    fi
    if [ -z "${API_KEY:-}" ]; then
        echo "ERROR: API_KEY is not set." >&2
        echo "       Fill in API_KEY= in $ENV_FILE" >&2
        exit 1
    fi
    if [ $# -eq 0 ]; then
        echo "ERROR: give a repo URL or --data <jsonl>." >&2
        exit 1
    fi

    write_mounted_env

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
            -e RETRIEVAL="$RETRIEVAL" \
            -e BUILDBENCH_ENV_FILE=/app/.env \
            -e PERFECT_RETRIEVAL="$PERFECT_RETRIEVAL" \
            -e RAG_RETRIEVAL="$RAG_RETRIEVAL" \
            -e MAX_TURNS="$MAX_TURNS" \
            -e TIMEOUT_BASH="$TIMEOUT_BASH" \
            -e AGENTS_NUMBER="$AGENTS_NUMBER" \
            -e REFINE_TIMES="$REFINE_TIMES" \
            -e CORES="$CORES" \
            -e OUTPUT_MODE=dir \
            -e BUILDBENCH_RUN_ID="$BUILDBENCH_RUN_ID" \
            -v "$MOUNTED_ENV:/app/.env:ro" \
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

Settings come from .env in the project root:

  cp .env.example .env     # then fill in API_KEY

The keys are mounted read-only into each container and read from the file there,
so they stay out of `docker inspect`.

Settings can also be given on the command line, BEFORE the script name; those
win over .env:

  MODEL_NAME=gemini-2.5-flash \
      scripts/run_container.sh run https://github.com/user/repo.git

  ENV_FILE       path to the settings file, default: <project>/.env
  API_KEY        LLM provider key                       (required for 'run')
  MODEL_NAME     default: o3-mini. Routed by substring: gpt/o3/o4 -> OpenAI,
                 claude -> Anthropic, gemini -> Google, deepseek -> DeepSeek,
                 qwen -> HuggingFace router. Any model id the provider accepts
                 works; this path does not restrict the name.
  LANGFUSE_SECRET_KEY / LANGFUSE_PUBLIC_KEY / LANGFUSE_BASE_URL
                 set them and every build is traced to Langfuse: one trace per
                 repo, holding the agent conversation, each LLM call and each
                 bash command, scored with the validation result. Unset, or
                 LANGFUSE_TRACING=false, turns tracing off.
  BUILDBENCH_RUN_ID  groups this invocation's repos into one Langfuse session,
                 default: a timestamp
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
