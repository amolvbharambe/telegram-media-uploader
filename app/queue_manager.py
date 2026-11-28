import os
import time
import random
import asyncio
import logging
from typing import Dict, Any, Optional, Set, Callable

from telethon import helpers
from . import db
from .config import load_config
from .telegram_client import telegram_manager
from .resumable_uploader import (
    ResumableUploader,
    FloodWaitException,
    PartsExpiredException,
    UploadCancelledException,
    UploadPausedException
)

logger = logging.getLogger("queue_manager")
logger.setLevel(logging.INFO)

def _is_network_error(exc: BaseException) -> bool:
    """Returns True if an exception represents a transient network/socket/connection failure."""
    if isinstance(exc, (ConnectionError, OSError, TimeoutError, asyncio.TimeoutError)):
        return True
    err_name = type(exc).__name__.lower()
    err_msg = str(exc).lower()
    return any(
        tok in err_name or tok in err_msg
        for tok in (
            "connection",
            "network",
            "timeout",
            "timedout",
            "timed out",
            "unreachable",
            "reset by peer",
            "broken pipe",
            "disconnected",
        )
    )

class QueueManager:
    def __init__(self):
        self._worker_task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()
        self._wakeup_event = asyncio.Event()
        self._subscribers: Set[asyncio.Queue] = set()

        # In-flight task control handles: task_id -> {cancel_event, pause_event, upload_task}
        self._active_tasks: Dict[int, Dict[str, Any]] = {}
        self._is_running = False

    def ensure_worker_alive(self):
        """Restarts the background queue worker if it stopped unexpectedly while not shutting down."""
        if not self._is_running or self._stop_event.is_set():
            return
        if self._worker_task is None or self._worker_task.done():
            logger.warning("Background upload worker stopped unexpectedly; restarting automatically...")
            self._worker_task = asyncio.create_task(self._worker_loop())

    async def start(self):
        """Starts the background queue processor worker."""
        if self._is_running and self._worker_task and not self._worker_task.done():
            return
        self._is_running = True
        self._stop_event.clear()
        self._worker_task = asyncio.create_task(self._worker_loop())
        logger.info("Background upload queue manager started.")

    async def stop(self):
        """Stops the background worker gracefully without cancelling resumable user tasks."""
        self._is_running = False
        self._stop_event.set()
        self._wakeup_event.set()
        for q in list(self._subscribers):
            try:
                q.put_nowait(None)
            except Exception:
                pass
        # Cancel active upload coroutines cleanly (leave DB status as uploading/queued to auto-resume)
        for task_id, data in list(self._active_tasks.items()):
            upload_task = data.get("upload_task")
            if upload_task and not upload_task.done():
                upload_task.cancel()

        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
        logger.info("Background upload queue manager stopped.")

    def trigger(self):
        """Wakes up the queue worker immediately (and self-heals if worker exited)."""
        self.ensure_worker_alive()
        self._wakeup_event.set()

    def subscribe(self) -> asyncio.Queue:
        """Subscribes to live queue and upload progress events."""
        q = asyncio.Queue()
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        """Removes a subscriber queue."""
        self._subscribers.discard(q)

    async def broadcast(self, event_type: str, data: Dict[str, Any]):
        """Broadcasts an event message to all connected WebSocket subscribers."""
        payload = {
            "type": event_type,
            "timestamp": time.time(),
            "data": data
        }
        for q in list(self._subscribers):
            try:
                q.put_nowait(payload)
            except Exception:
                self._subscribers.discard(q)

    async def _get_authorized_client(self):
        """Returns (client, is_authorized), forcing reconnect if the socket check fails."""
        try:
            client = await telegram_manager.get_client()
            if not client:
                return None, False
            is_auth = await asyncio.wait_for(client.is_user_authorized(), timeout=10.0)
            return client, bool(is_auth)
        except asyncio.CancelledError:
            if self._stop_event.is_set():
                raise
            logger.warning("Auth readiness check cancelled by connection reset; reconnecting...")
            await telegram_manager.reconnect()
            return None, False
        except Exception as e:
            logger.warning(f"Telegram connection/auth check error: {e}")
            await telegram_manager.reconnect()
            return None, False

    async def _wait_or_wakeup(self, timeout: float):
        self._wakeup_event.clear()
        try:
            await asyncio.wait_for(self._wakeup_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass

    async def _handle_task_failure(self, task_id: int, e: BaseException, base_delay: float, max_delay: float):
        """Handles upload errors with clean network reconnection and persistent retry backoff."""
        logger.error(f"Error on task #{task_id}: {e}", exc_info=True)
        current_task = await db.get_task(task_id)
        if not current_task:
            return

        is_net_err = _is_network_error(e)
        retry_count = current_task.get("retry_count", 0) + 1
        max_retries = current_task.get("max_retries", 10)

        # Transient network/connection outages should keep auto-retrying cleanly instead of
        # failing permanently during prolonged offline periods.
        if is_net_err or retry_count <= max_retries:
            cap_delay = min(max_delay, 30.0) if is_net_err else max_delay
            delay = min(cap_delay, base_delay * (2 ** min(retry_count - 1, 6)))
            jitter = random.uniform(0.5, 2.0)
            total_delay = round(delay + jitter, 1)
            next_retry = time.time() + total_delay

            err_msg = f"{type(e).__name__}: {str(e)}"
            await db.increment_task_retry(task_id, err_msg, next_retry)
            logger.info(
                f"Task #{task_id} will auto-resume in {total_delay}s "
                f"(Attempt {retry_count}{'' if is_net_err else f'/{max_retries}'})"
            )
            await self.broadcast("task_retrying", {
                "task_id": task_id,
                "error": err_msg,
                "retry_count": retry_count,
                "max_retries": max_retries,
                "retry_in": total_delay
            })

            if is_net_err:
                # Proactively rebuild the MTProto connection and wait briefly before picking another task
                reconnected = await telegram_manager.reconnect()
                if not reconnected or not reconnected.is_connected():
                    await self.broadcast("status_update", {
                        "message": "Network disconnected. Waiting to reconnect...",
                        "waiting_for_connection": True
                    })
                    await self._wait_or_wakeup(5.0)
        else:
            logger.error(f"Task #{task_id} exceeded maximum retries ({max_retries}). Marked failed.")
            await db.update_task_status(task_id, status="failed", error_message=str(e))
            await self.broadcast("task_failed", {
                "task_id": task_id,
                "error": f"Exceeded max retries: {str(e)}"
            })

    async def _worker_loop(self):
        """Continuous background worker loop that processes pending and retrying uploads."""
        cfg = load_config()
        base_delay = float(cfg.get("retry_base_delay", 2.0))
        max_delay = float(cfg.get("retry_max_delay", 60.0))

        logger.info("Queue worker loop initialized. Checking for resumable tasks...")

        while not self._stop_event.is_set():
            try:
                # 1. Fetch pending, interrupted, or ready-to-retry tasks
                tasks = await db.get_pending_or_resumable_tasks()
                now = time.time()
                runnable_task = None

                for t in tasks:
                    status = t["status"]
                    if status == "retrying":
                        next_retry = t.get("next_retry_at") or 0
                        if now >= next_retry:
                            runnable_task = t
                            break
                    elif status in ("queued", "uploading"):
                        runnable_task = t
                        break

                if not runnable_task:
                    await self._wait_or_wakeup(3.0)
                    continue

                # 2. Check if Telegram client is connected and ready
                client, is_auth = await self._get_authorized_client()
                if not client or not is_auth:
                    await self.broadcast("status_update", {
                        "message": "Waiting for Telegram connection & authorization...",
                        "waiting_for_auth": True
                    })
                    await self._wait_or_wakeup(3.0)
                    continue

                # 3. Process the task
                task_id = runnable_task["id"]
                logger.info(f"Processing task #{task_id}: {runnable_task['file_name']} (Status: {runnable_task['status']})")

                cancel_event = asyncio.Event()
                pause_event = asyncio.Event()
                self._active_tasks[task_id] = {
                    "cancel_event": cancel_event,
                    "pause_event": pause_event,
                    "upload_task": None
                }

                await db.update_task_status(task_id, status="uploading", clear_error=True)
                await self.broadcast("task_started", {"task_id": task_id, "task": runnable_task})

                async def progress_cb(info: Dict[str, Any]):
                    await self.broadcast("progress", info)

                uploader = ResumableUploader(
                    client=client,
                    task_id=task_id,
                    progress_callback=progress_cb,
                    cancel_event=cancel_event,
                    pause_event=pause_event,
                    shutdown_event=self._stop_event,
                    max_concurrent=int(cfg.get("max_concurrent_chunks", 3))
                )

                try:
                    upload_task = asyncio.create_task(uploader.upload())
                    self._active_tasks[task_id]["upload_task"] = upload_task
                    try:
                        message_id = await upload_task
                    except asyncio.CancelledError:
                        if self._stop_event.is_set():
                            raise
                        if cancel_event.is_set():
                            raise UploadCancelledException("Upload cancelled by user.")
                        if pause_event.is_set():
                            raise UploadPausedException("Upload paused by user.")
                        raise ConnectionError("Upload interrupted by connection reset")

                    completed_task = await db.get_task(task_id)
                    await self.broadcast("task_completed", {
                        "task_id": task_id,
                        "message_id": message_id,
                        "task": completed_task
                    })
                    logger.info(f"Task #{task_id} completed successfully (Msg ID: {message_id})")

                    # Check auto-delete local file if configured
                    if cfg.get("auto_delete_local_on_finish") and os.path.exists(runnable_task["file_path"]):
                        from .config import UPLOADS_DIR
                        if str(UPLOADS_DIR) in runnable_task["file_path"]:
                            try:
                                os.remove(runnable_task["file_path"])
                            except Exception as e:
                                logger.warning(f"Failed to auto-delete {runnable_task['file_path']}: {e}")

                except FloodWaitException as e:
                    wait_sec = e.seconds + 2
                    logger.warning(f"Task #{task_id} FloodWait: waiting {wait_sec}s before auto-resume")
                    next_retry = time.time() + wait_sec
                    await db.increment_task_retry(task_id, f"FloodWait: resuming in {wait_sec}s", next_retry)
                    await self.broadcast("task_retrying", {
                        "task_id": task_id,
                        "error": f"Telegram rate limit: resuming in {wait_sec}s",
                        "retry_in": wait_sec
                    })

                except PartsExpiredException:
                    logger.warning(f"Task #{task_id} remote parts expired. Regenerating file ID and resuming...")
                    new_file_id = helpers.generate_random_long()
                    await db.reset_task_parts(task_id, new_file_id)
                    next_retry = time.time() + 1.0
                    await db.increment_task_retry(task_id, "Parts expired on DC. Automatically restarting...", next_retry)
                    await self.broadcast("task_retrying", {
                        "task_id": task_id,
                        "error": "Telegram parts expired. Automatically re-uploading...",
                        "retry_in": 1.0
                    })

                except UploadCancelledException:
                    logger.info(f"Task #{task_id} was cancelled by user.")
                    await db.update_task_status(task_id, status="cancelled", error_message="Cancelled by user")
                    await self.broadcast("task_cancelled", {"task_id": task_id})

                except UploadPausedException:
                    logger.info(f"Task #{task_id} was paused by user.")
                    await db.update_task_status(task_id, status="paused")
                    await self.broadcast("task_paused", {"task_id": task_id})

                except Exception as e:
                    await self._handle_task_failure(task_id, e, base_delay, max_delay)

                finally:
                    self._active_tasks.pop(task_id, None)

            except asyncio.CancelledError:
                if self._stop_event.is_set():
                    break
                logger.warning("Queue worker loop caught unexpected CancelledError; staying alive...")
                await asyncio.sleep(1.0)
            except Exception as loop_err:
                logger.error(f"Unexpected error in queue worker loop: {loop_err}", exc_info=True)
                await asyncio.sleep(2.0)

    def _signal_inflight(self, task_id: int, event_key: str):
        """Sets pause or cancel flag on an active task and cancels any hung network await immediately."""
        active = self._active_tasks.get(task_id)
        if not active:
            return
        active[event_key].set()
        upload_task = active.get("upload_task")
        if upload_task and not upload_task.done():
            upload_task.cancel()

    async def pause_task(self, task_id: int):
        self._signal_inflight(task_id, "pause_event")
        await db.update_task_status(task_id, status="paused")
        self.ensure_worker_alive()
        await self.broadcast("task_paused", {"task_id": task_id})

    async def resume_task(self, task_id: int):
        await db.update_task_status(
            task_id, status="queued", next_retry_at=0, clear_error=True, reset_retry=True
        )
        self.trigger()
        await self.broadcast("task_resumed", {"task_id": task_id})

    async def retry_task_now(self, task_id: int):
        await db.update_task_status(
            task_id, status="queued", next_retry_at=0, clear_error=True, reset_retry=True
        )
        self.trigger()
        await self.broadcast("task_retrying", {"task_id": task_id, "retry_in": 0})

    async def cancel_task(self, task_id: int):
        self._signal_inflight(task_id, "cancel_event")
        await db.update_task_status(task_id, status="cancelled", error_message="Cancelled by user")
        self.ensure_worker_alive()
        await self.broadcast("task_cancelled", {"task_id": task_id})

queue_manager = QueueManager()
