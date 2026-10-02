"""Run risky native work (parsing untrusted repository files) in a throw-away child process.

Why: tree-sitter is native code and it parses attacker-influenced files. A parser crash (access violation) or a
pathological input that never finishes must cost one review's evidence, not the whole webhook server. The child is
started with ``spawn`` (no inherited state), killed on timeout, and any failure is reported as ``IsolationError``.
"""

from __future__ import annotations

import multiprocessing
from typing import Any, Callable


class IsolationError(RuntimeError):
    """The worker crashed, timed out or raised; the message never contains repository content."""


def _child(connection: Any, function: Callable[..., Any], args: tuple[Any, ...]) -> None:
    try:
        connection.send((True, function(*args)))
    except BaseException as error:  # report the type only
        try:
            connection.send((False, type(error).__name__))
        except Exception:
            pass
    finally:
        connection.close()


def run_isolated(function: Callable[..., Any], args: tuple[Any, ...], timeout: float) -> Any:
    """Call ``function(*args)`` in a fresh process and return its (picklable) result.

    ``function`` must be importable at module level; ``args`` and the result must be picklable.
    """
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=_child, args=(sender, function, args), daemon=True)
    process.start()
    sender.close()  # the parent only reads; this also lets recv() see EOF if the child dies
    try:
        if not receiver.poll(timeout):
            raise IsolationError(f"worker did not finish within {timeout:.0f}s")
        try:
            ok, payload = receiver.recv()
        except (EOFError, OSError) as error:
            raise IsolationError("worker process crashed") from error
        if not ok:
            raise IsolationError(f"worker failed ({payload})")
        return payload
    finally:
        receiver.close()
        if process.is_alive():
            process.kill()
        process.join(5)
