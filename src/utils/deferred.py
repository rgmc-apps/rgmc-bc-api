"""run_deferred — timeout-aware deferred result helper.

Usage in a route handler (sync def):

    from src.utils.deferred import run_deferred
    from fastapi.responses import JSONResponse

    @router.get("/bc/custom/v2/customers")
    def list_customers(company: str = Query(...)):
        key, data = run_deferred(
            fn=lambda: {"data": fetch_customers(company)},
            endpoint="/bc/custom/v2/customers",
            params={"company": company},
        )
        if key:
            return JSONResponse(
                status_code=202,
                content={"status": "deferred", "key": key, "poll_url": f"/deferred/{key}"},
            )
        return data

How it works:
  1. fn() runs in a daemon thread.
  2. The calling thread waits up to `soft_timeout` seconds.
  3. If fn() finishes in time  → return (None, result); caller handles normally.
  4. If fn() is still running  → create a Firestore "pending" record, return (key, None).
     A separate background thread waits for fn() to finish, then writes the result
     to GCS and marks the Firestore record as "ready". Client polls GET /deferred/{key}.

After the 202 is returned to the client, Gunicorn's per-request timer resets, so
the background thread runs to completion without risk of a worker timeout.
"""
import logging
import threading
from typing import Any, Callable

from src.services.deferred_result_service import create_pending, fail_result, save_result

logger = logging.getLogger("deferred")

_DEFAULT_SOFT_TIMEOUT = 120.0  # seconds — must be well under Gunicorn's 180 s limit


def run_deferred(
    fn: Callable[[], Any],
    endpoint: str,
    params: dict,
    soft_timeout: float = _DEFAULT_SOFT_TIMEOUT,
) -> tuple[str | None, Any]:
    """Run fn() with a soft timeout.

    Returns:
        (None, result)  — fn() finished within soft_timeout; result is fn()'s return value.
        (key, None)     — fn() is still running; key is a Firestore deferred record ID.
                          A background thread will write the result when fn() finishes.

    Raises if fn() raises within soft_timeout (fast-path errors propagate normally).
    """
    result_holder: dict[str, Any] = {}
    error_holder: dict[str, str] = {}
    done = threading.Event()

    def _worker():
        try:
            result_holder["data"] = fn()
        except Exception as exc:
            error_holder["err"] = str(exc)
            error_holder["exc"] = exc  # type: ignore[assignment]
        finally:
            done.set()

    worker_thread = threading.Thread(target=_worker, daemon=True)
    worker_thread.start()

    finished_in_time = done.wait(timeout=soft_timeout)

    if finished_in_time:
        if "exc" in error_holder:
            raise error_holder["exc"]  # type: ignore[misc]
        return None, result_holder["data"]

    # fn() is still running — defer the result.
    key = create_pending(endpoint, params)
    logger.info(
        f"Deferring slow request: endpoint={endpoint!r} key={key!r} "
        f"soft_timeout={soft_timeout}s"
    )

    def _background_saver():
        done.wait()  # blocks until _worker() sets the event
        if "exc" in error_holder:
            fail_result(key, error_holder["err"])
            logger.error(f"Deferred fn() raised: key={key!r} error={error_holder['err']!r}")
        else:
            try:
                save_result(key, result_holder["data"])
            except Exception as exc:
                fail_result(key, str(exc))
                logger.error(f"Deferred save raised: key={key!r} error={exc!r}")

    threading.Thread(target=_background_saver, daemon=True).start()
    return key, None
