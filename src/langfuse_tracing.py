"""
Langfuse tracing for the compilation agents.

One trace per repository build. Inside it:

    repo build (span)          <- compilation.py, named after the repo
      retrieval-chat (span)    <- agents.py, only in the 3-agent setup
        OpenAI-generation      <- every LLM call, with prompts and token usage
        bash (span)            <- every command the executor runs
      compilation-chat (span)
        ...

Two things are worth knowing about how this is wired.

*Credentials never enter the environment.* `env_config` reads them from the
mounted settings file and they are passed to the Langfuse constructor directly.
Putting them in `os.environ` would hand them to every configure/make/setup.py
the agent runs as a subprocess -- the same reasoning as in src/env_config.py.

*LLM calls are captured by patching the SDKs, not AutoGen.* Importing
`langfuse.openai` patches the OpenAI client in place, and AutoGen reaches
OpenAI, DeepSeek, Gemini and Qwen through it, so no AutoGen-specific
instrumentation is needed (the published one targets AutoGen >= 0.4, a different
API from the pyautogen 0.3 used here). Claude goes through the anthropic SDK
instead, instrumented separately when the optional
opentelemetry-instrumentation-anthropic package is present.

Tracing is best-effort by construction: without keys, without the package, or on
any error it turns itself off and the build proceeds untraced. A build must
never fail because an observability backend is unhappy.
"""

import os
import socket
import time
from contextlib import contextmanager

import env_config

### Long bash output would otherwise dominate the trace payload. The head is
### what identifies the command; the tail is where the error message is, so
### keep both and drop the middle.
_MAX_IO_CHARS = 4000

_client = None
_run_id = None
_enabled = False
_initialised = False


def _truncate(text):
    if not isinstance(text, str) or len(text) <= _MAX_IO_CHARS:
        return text
    head = _MAX_IO_CHARS // 2
    tail = _MAX_IO_CHARS - head
    omitted = len(text) - _MAX_IO_CHARS
    return f"{text[:head]}\n... [{omitted} characters omitted] ...\n{text[-tail:]}"


def resolve_run_id():
    """
    Identifier shared by every repo of one experiment; becomes the Langfuse
    session id, so a whole run reads as one session in the UI.

    It also seeds the trace ids, so it has to be identical in the worker and in
    the compilation subprocess it spawns, and it has to *change* between runs --
    otherwise two runs of the same repo would write into one trace. Exported to
    the environment so children inherit it.
    """
    global _run_id
    if _run_id:
        return _run_id

    _run_id = env_config.get("BUILDBENCH_RUN_ID") or env_config.get("LANGFUSE_SESSION_ID")
    if not _run_id:
        ### Distinct per container and per start, which is what the seeding needs.
        _run_id = f"{socket.gethostname()}-{time.strftime('%Y-%m-%d_%H-%M-%S')}"
    os.environ["BUILDBENCH_RUN_ID"] = _run_id
    return _run_id


### Everything a child container needs to trace into the same run.
FORWARDED_KEYS = (
    "LANGFUSE_PUBLIC_KEY",
    "LANGFUSE_SECRET_KEY",
    "LANGFUSE_BASE_URL",
    "LANGFUSE_TRACING",
    "LANGFUSE_ENVIRONMENT",
)


def forwarded_env():
    """
    Tracing configuration to hand to a container this process starts, for the
    paths that have no mounted settings file (src/main.py, orchestrator.py).
    The run id is included so every repo of one experiment shares a session.
    """
    forwarded = {"BUILDBENCH_RUN_ID": run_id()}
    for key in FORWARDED_KEYS:
        value = env_config.get(key)
        if value:
            forwarded[key] = value
    return forwarded


def init(quiet=False):
    """
    Set up the Langfuse client and instrument the LLM SDKs. Idempotent; returns
    whether tracing is on.
    """
    global _client, _enabled, _initialised
    if _initialised:
        return _enabled
    _initialised = True

    resolve_run_id()

    if not env_config.get_bool("LANGFUSE_TRACING", True):
        if not quiet:
            print("[langfuse] tracing disabled (LANGFUSE_TRACING is false)")
        return False

    public_key = env_config.get("LANGFUSE_PUBLIC_KEY")
    secret_key = env_config.get("LANGFUSE_SECRET_KEY")
    if not public_key or not secret_key:
        if not quiet:
            print("[langfuse] tracing off: LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY not set")
        return False

    base_url = env_config.get("LANGFUSE_BASE_URL", "https://cloud.langfuse.com")

    try:
        from langfuse import Langfuse

        _client = Langfuse(
            public_key=public_key,
            secret_key=secret_key,
            base_url=base_url,
            environment=env_config.get("LANGFUSE_ENVIRONMENT", "default"),
        )

        ### Patches the OpenAI SDK in place. Bound to a throwaway name on
        ### purpose: `import langfuse.openai` would rebind `langfuse` here.
        from langfuse.openai import openai as _patched_openai  # noqa: F401

        _enabled = True
    except Exception as e:
        print(f"[langfuse] tracing off: could not initialise ({type(e).__name__}: {e})")
        _client = None
        return False

    ### Optional, and only relevant for Claude: every other provider this repo
    ### routes to speaks the OpenAI protocol and is already covered above.
    try:
        from opentelemetry.instrumentation.anthropic import AnthropicInstrumentor

        AnthropicInstrumentor().instrument()
    except ImportError:
        pass
    except Exception as e:
        print(f"[langfuse] anthropic instrumentation skipped ({type(e).__name__}: {e})")

    if not quiet:
        print(f"[langfuse] tracing to {base_url}, run id (session) {_run_id}")
    return True


def enabled():
    return _enabled


def run_id():
    return _run_id or resolve_run_id()


def trace_id_for(repo_name):
    """
    Trace id for one repo build, derived from (run id, repo name).

    Deterministic on purpose: the build runs in compilation.py and the
    validation that scores it runs in the parent process, so both need to name
    the same trace without passing anything between them.
    """
    if not _enabled:
        return None
    try:
        from langfuse import Langfuse

        return Langfuse.create_trace_id(seed=f"{run_id()}:{repo_name}")
    except Exception:
        return None


class _NullSpan:
    """Stand-in span, so call sites need no `if enabled()` around them."""

    def update(self, **kwargs):
        pass


@contextmanager
def repo_build(repo_name, repo_url=None, metadata=None, tags=None):
    """Root span of a repo build; everything the agents do lands underneath it."""
    if not _enabled:
        yield _NullSpan()
        return

    try:
        from langfuse import propagate_attributes
    except Exception:
        yield _NullSpan()
        return

    trace_id = trace_id_for(repo_name)
    ### Opening the span is what can fail on the Langfuse side; the body must
    ### not be inside that except, or a failing build would be swallowed here.
    try:
        span_cm = _client.start_as_current_observation(
            as_type="span",
            name=repo_name,
            input={"repo_url": repo_url} if repo_url else None,
            metadata=metadata,
            trace_context={"trace_id": trace_id} if trace_id else None,
        )
        attributes_cm = propagate_attributes(
            session_id=run_id(), tags=tags, metadata=metadata
        )
    except Exception as e:
        print(f"[langfuse] could not open trace for {repo_name} ({type(e).__name__}: {e})")
        yield _NullSpan()
        return

    try:
        with span_cm as span, attributes_cm:
            try:
                yield span
            except Exception as e:
                update(span, level="ERROR", status_message=f"{type(e).__name__}: {e}")
                raise
    finally:
        flush()


@contextmanager
def step(name, as_type="span", input=None, metadata=None):
    """Child span: a chat phase, a bash command, anything worth seeing nested."""
    if not _enabled:
        yield _NullSpan()
        return
    try:
        span_cm = _client.start_as_current_observation(
            as_type=as_type,
            name=name,
            input=_truncate(input),
            metadata=metadata,
        )
    except Exception as e:
        print(f"[langfuse] span '{name}' error ({type(e).__name__}: {e})")
        yield _NullSpan()
        return

    with span_cm as span:
        yield span


def update(span, **kwargs):
    """Set fields on a span, truncating input/output. No-op if tracing is off."""
    if span is None:
        return
    try:
        for key in ("input", "output"):
            if key in kwargs:
                kwargs[key] = _truncate(kwargs[key])
        ### `level=None` at a call site means "nothing to say", not "clear it".
        span.update(**{k: v for k, v in kwargs.items() if v is not None})
    except Exception:
        pass


def score(repo_name, name, value, comment=None, data_type=None):
    """
    Attach a score to a repo's trace. Callable from a process that did not
    create the trace -- the id is derived, not remembered.
    """
    if not _enabled:
        return
    trace_id = trace_id_for(repo_name)
    if not trace_id:
        return
    try:
        _client.create_score(
            trace_id=trace_id, name=name, value=value, comment=comment, data_type=data_type
        )
    except Exception as e:
        print(f"[langfuse] could not send score '{name}' ({type(e).__name__}: {e})")


def flush():
    if _enabled and _client is not None:
        try:
            _client.flush()
        except Exception:
            pass


def shutdown():
    """Flush and stop. Required: these are short-lived processes."""
    global _enabled
    if _enabled and _client is not None:
        try:
            _client.shutdown()
        except Exception as e:
            print(f"[langfuse] shutdown error ({type(e).__name__}: {e})")
    _enabled = False
