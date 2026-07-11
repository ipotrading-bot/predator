"""
tests/test_run_engine_import.py — run_engine.py must not crash when
imported outside the main thread.

Before the fix, `signal.signal(signal.SIGALRM, ...)` / `signal.alarm(...)`
ran at MODULE level (import time) — Python's signal module only allows
signal.signal() to be called from the main thread of the main interpreter,
so any code path that imports run_engine from a worker thread (a test
runner, a dashboard route handling a request off-thread, a future async
wrapper) raised ValueError before a single line of run()'s actual logic
executed.

Uses a real subprocess (not a plain threading.Thread in-process) because
pytest's own collection already imports run_engine in the main thread
earlier in the same process — sys.modules would cache it and reimporting
in a thread here would silently no-op instead of re-executing module-level
code, hiding the exact bug this guards against.
"""
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

_SCRIPT = """
import threading

error = []

def worker():
    try:
        import run_engine  # noqa: F401
    except Exception as e:
        error.append(e)

t = threading.Thread(target=worker)
t.start()
t.join(timeout=10)

if error:
    raise SystemExit(f"import raised in worker thread: {error[0]!r}")
if t.is_alive():
    raise SystemExit("worker thread never finished (hung import)")
print("OK")
"""


def test_import_in_worker_thread_does_not_raise():
    result = subprocess.run(
        [sys.executable, "-c", _SCRIPT],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "OK" in result.stdout
