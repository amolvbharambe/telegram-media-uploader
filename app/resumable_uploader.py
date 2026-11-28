import os
import time
import hashlib
import asyncio
import logging
from typing import Optional, Callable, Dict, Any

from telethon import TelegramClient, functions, types, errors, helpers, utils
from . import db

logger = logging.getLogger("resumable_uploader")
logger.setLevel(logging.INFO)

class FloodWaitException(Exception):
    def __init__(self, seconds: int):
        self.seconds = seconds
        super().__init__(f"Telegram FloodWait: must wait {seconds} seconds")

class PartsExpiredException(Exception):
    """Raised when Telegram reports file parts are invalid or expired."""
    pass

class UploadCancelledException(Exception):
    pass

class UploadPausedException(Exception):
    pass

class ResumableUploader:
    PART_UPLOAD_TIMEOUT = 45.0
    ENTITY_RESOLVE_TIMEOUT = 30.0
    SEND_FILE_TIMEOUT = 180.0

    def __init__(
        self,
        client: TelegramClient,
        task_id: int,
        progress_callback: Optional[Callable[[Dict[str, Any]], Any]] = None,
        cancel_event: Optional[asyncio.Event] = None,
        pause_event: Optional[asyncio.Event] = None,
        shutdown_event: Optional[asyncio.Event] = None,
        max_concurrent: int = 3
    ):
        self.client = client
        self.task_id = task_id
        self.progress_callback = progress_callback
        self.cancel_event = cancel_event or asyncio.Event()
        self.pause_event = pause_event
        self.shutdown_event = shutdown_event
        self.max_concurrent = max_concurrent

        # Speed calculation variables
        self._start_time = time.time()
        self._last_time = time.time()
        self._last_bytes = 0
        self._current_speed = 0.0  # bytes/sec

    def _raise_for_control_state(self, context: str):
        """Maps cancellation/control flags cleanly without letting Telethon disconnects abort the worker."""
        if self.shutdown_event and self.shutdown_event.is_set():
            raise asyncio.CancelledError()
        if self.cancel_event.is_set():
            raise UploadCancelledException("Upload cancelled by user.")
        if self.pause_event and self.pause_event.is_set():
            raise UploadPausedException("Upload paused by user.")
        raise ConnectionError(f"Telegram connection interrupted during {context}")

    async def _await_network(self, coro, timeout: float, context: str):
        """Awaits a Telethon network operation with a bounded timeout and disconnect guard."""
        try:
            return await asyncio.wait_for(coro, timeout=timeout)
        except asyncio.TimeoutError:
            raise ConnectionError(f"Timed out during {context} after {int(timeout)}s")
        except asyncio.CancelledError:
            self._raise_for_control_state(context)

    async def upload(self) -> int:
        """
        Executes or resumes chunked upload for the given task.
        Returns the Telegram message ID upon completion.
        """
        task = await db.get_task(self.task_id)
        if not task:
            raise ValueError(f"Task {self.task_id} not found in database.")

        file_path = task["file_path"]
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"File {file_path} does not exist on disk.")

        # Ensure MP4 faststart for instant streaming if needed
        force_document = bool(task["force_document"])
        media_type = task["media_type"]
        if not force_document and media_type in ("auto", "video"):
            from . import media_utils
            file_path = media_utils.ensure_mp4_faststart(file_path)

        file_size = os.path.getsize(file_path)
        if file_size != task["file_size"]:
            logger.warning(
                f"File size on disk ({file_size}) differs from task ({task['file_size']})."
            )

        chunk_size = task["chunk_size"]
        total_parts = task["total_parts"]
        file_id = task["file_id"]
        file_name = task["file_name"]
        is_big = file_size > 10 * 1024 * 1024  # > 10 MB

        # If not big, calculate MD5 of the whole file
        md5_checksum = ""
        if not is_big:
            hasher = hashlib.md5()
            with open(file_path, "rb") as f:
                while chunk := f.read(1024 * 1024):
                    hasher.update(chunk)
            md5_checksum = hasher.hexdigest()

        # Check which parts have already been uploaded
        completed_parts = await db.get_completed_parts(self.task_id)
        logger.info(
            f"Task {self.task_id}: {len(completed_parts)}/{total_parts} parts already uploaded."
        )

        remaining_parts = [p for p in range(total_parts) if p not in completed_parts]

        # Calculate initial bytes uploaded
        uploaded_bytes = len(completed_parts) * chunk_size
        if (total_parts - 1) in completed_parts:
            # Last part might be smaller than chunk_size
            last_part_size = file_size - ((total_parts - 1) * chunk_size)
            uploaded_bytes = (len(completed_parts) - 1) * chunk_size + last_part_size
        self._last_bytes = uploaded_bytes

        # If there are chunks remaining to upload
        if remaining_parts:
            await self._upload_parts(
                part_indices=remaining_parts,
                file_path=file_path,
                file_size=file_size,
                chunk_size=chunk_size,
                total_parts=total_parts,
                file_id=file_id,
                is_big=is_big,
                completed_count=len(completed_parts),
                uploaded_bytes=uploaded_bytes,
            )

        # All parts are uploaded! Construct InputFile handle
        if is_big:
            input_file = types.InputFileBig(
                id=file_id,
                parts=total_parts,
                name=file_name
            )
        else:
            input_file = types.InputFile(
                id=file_id,
                parts=total_parts,
                name=file_name,
                md5_checksum=md5_checksum
            )

        # Resolve recipient entity
        target_chat = task["target_chat"]
        target_entity = await self._await_network(
            self.resolve_entity(target_chat),
            timeout=self.ENTITY_RESOLVE_TIMEOUT,
            context=f"resolving target '{target_chat}'"
        )

        # Prepare send_file options
        force_document = bool(task["force_document"])
        caption = task["caption"] or ""
        media_type = task["media_type"]

        if media_type == "document":
            force_document = True

        from . import media_utils
        attributes, thumb_path, supports_streaming, mime_type = (
            media_utils.prepare_telegram_media_attributes(
                file_path=file_path,
                file_name=file_name,
                media_type=media_type,
                force_document=force_document
            )
        )

        # If sending an image in exact file size mode, send as document with DocumentAttributeImageSize
        # so Telegram servers never compress or re-encode the original image bytes!
        if any(isinstance(a, types.DocumentAttributeImageSize) for a in attributes):
            force_document = True

        logger.info(
            f"Sending file to target: {target_chat} | streaming={supports_streaming} "
            f"| force_document={force_document} | mime={mime_type} | thumb={bool(thumb_path)}"
        )

        send_kwargs = {
            "entity": target_entity,
            "file": input_file,
            "caption": caption,
            "force_document": force_document,
            "attributes": attributes,
            "supports_streaming": supports_streaming,
            "mime_type": mime_type
        }
        if thumb_path and os.path.exists(thumb_path):
            send_kwargs["thumb"] = thumb_path

        # Deliver message; repair single missing DC parts inline, or reset file_id if expired
        repaired_parts = set()
        while True:
            try:
                sent_message = await self._await_network(
                    self.client.send_file(**send_kwargs),
                    timeout=self.SEND_FILE_TIMEOUT,
                    context=f"finalizing message delivery for {file_name}"
                )
                break
            except errors.FilePartMissingError as e:
                missing_idx = getattr(e, "which", None)
                if (
                    isinstance(missing_idx, int)
                    and 0 <= missing_idx < total_parts
                    and missing_idx not in repaired_parts
                    and len(repaired_parts) < 3
                ):
                    repaired_parts.add(missing_idx)
                    logger.warning(
                        f"Task {self.task_id}: Part {missing_idx} missing from Telegram storage "
                        f"during SendMediaRequest. Re-uploading part {missing_idx} ({len(repaired_parts)}/3)..."
                    )
                    await db.delete_task_part(self.task_id, missing_idx)
                    cur_completed = await db.get_completed_parts(self.task_id)
                    part_len = min(chunk_size, file_size - (missing_idx * chunk_size))
                    await self._upload_parts(
                        part_indices=[missing_idx],
                        file_path=file_path,
                        file_size=file_size,
                        chunk_size=chunk_size,
                        total_parts=total_parts,
                        file_id=file_id,
                        is_big=is_big,
                        completed_count=len(cur_completed),
                        uploaded_bytes=max(0, file_size - part_len),
                    )
                    continue

                logger.error(
                    f"Task {self.task_id}: Multiple/expired parts missing on Telegram DC ({e}). "
                    "Resetting upload with a fresh file ID..."
                )
                raise PartsExpiredException(
                    f"Part {missing_idx} missing on Telegram storage; restarting upload."
                ) from e
            except errors.FilePartsInvalidError as e:
                logger.error(f"Task {self.task_id}: Uploaded parts rejected by Telegram DC ({e}).")
                raise PartsExpiredException("Uploaded parts invalid on Telegram DC.") from e

        message_id = sent_message.id if hasattr(sent_message, "id") else None
        logger.info(f"Task {self.task_id} successfully sent! Telegram message ID: {message_id}")

        await db.update_task_status(
            self.task_id,
            status="completed",
            message_id=message_id,
            clear_error=True
        )

        return message_id

    async def _upload_parts(
        self,
        part_indices: list,
        file_path: str,
        file_size: int,
        chunk_size: int,
        total_parts: int,
        file_id: int,
        is_big: bool,
        completed_count: int,
        uploaded_bytes: int,
    ):
        """Concurrently uploads the specified chunk indices with timeout and DB persistence."""
        semaphore = asyncio.Semaphore(self.max_concurrent)
        lock = asyncio.Lock()
        failed_event = asyncio.Event()
        completed_parts_count = completed_count
        current_bytes = uploaded_bytes

        async def upload_part(part_index: int):
            if failed_event.is_set():
                return
            if self.shutdown_event and self.shutdown_event.is_set():
                raise asyncio.CancelledError()
            if self.cancel_event.is_set():
                raise UploadCancelledException("Upload cancelled by user.")
            if self.pause_event and self.pause_event.is_set():
                raise UploadPausedException("Upload paused by user.")

            part_offset = part_index * chunk_size
            part_len = min(chunk_size, file_size - part_offset)

            with open(file_path, "rb") as f:
                f.seek(part_offset)
                part_bytes = f.read(part_len)

            if len(part_bytes) != part_len:
                failed_event.set()
                raise IOError(
                    f"Expected {part_len} bytes for part {part_index}, read {len(part_bytes)}"
                )

            async with semaphore:
                if failed_event.is_set():
                    return
                if self.shutdown_event and self.shutdown_event.is_set():
                    raise asyncio.CancelledError()
                if self.cancel_event.is_set():
                    raise UploadCancelledException("Upload cancelled by user.")
                if self.pause_event and self.pause_event.is_set():
                    raise UploadPausedException("Upload paused by user.")

                try:
                    if is_big:
                        req = functions.upload.SaveBigFilePartRequest(
                            file_id=file_id,
                            file_part=part_index,
                            file_total_parts=total_parts,
                            bytes=part_bytes
                        )
                    else:
                        req = functions.upload.SaveFilePartRequest(
                            file_id=file_id,
                            file_part=part_index,
                            bytes=part_bytes
                        )

                    res = await self._await_network(
                        self.client(req),
                        timeout=self.PART_UPLOAD_TIMEOUT,
                        context=f"uploading part {part_index}"
                    )
                    if not res:
                        raise RuntimeError(f"SavePartRequest returned False for part {part_index}")

                except errors.FloodWaitError as e:
                    failed_event.set()
                    logger.warning(f"FloodWait encountered: {e.seconds}s on task {self.task_id}")
                    raise FloodWaitException(e.seconds)
                except (errors.FilePartMissingError, errors.FilePartsInvalidError) as e:
                    failed_event.set()
                    logger.error(f"Parts invalid on Telegram: {e}")
                    raise PartsExpiredException("Parts expired or invalid on Telegram DC.")
                except asyncio.CancelledError:
                    self._raise_for_control_state(f"uploading part {part_index}")
                except Exception as e:
                    failed_event.set()
                    raise e

                await db.record_part_upload(self.task_id, part_index, part_len, time.time())

                async with lock:
                    nonlocal current_bytes, completed_parts_count
                    current_bytes = min(file_size, current_bytes + part_len)
                    completed_parts_count = min(total_parts, completed_parts_count + 1)
                    now = time.time()
                    dt = now - self._last_time
                    if dt >= 0.5:
                        db_bytes = current_bytes - self._last_bytes
                        self._current_speed = db_bytes / dt if dt > 0 else 0.0
                        self._last_time = now
                        self._last_bytes = current_bytes

                    eta = 0.0
                    if self._current_speed > 0:
                        eta = max(0.0, (file_size - current_bytes) / self._current_speed)

                    if self.progress_callback:
                        try:
                            res = self.progress_callback({
                                "task_id": self.task_id,
                                "uploaded_bytes": current_bytes,
                                "total_bytes": file_size,
                                "completed_parts": completed_parts_count,
                                "total_parts": total_parts,
                                "speed": self._current_speed,
                                "eta": eta,
                                "percent": round((current_bytes / file_size) * 100, 2)
                            })
                            if asyncio.iscoroutine(res):
                                await res
                        except Exception as e:
                            logger.debug(f"Progress callback error: {e}")

        tasks = [asyncio.create_task(upload_part(p)) for p in part_indices]
        try:
            await asyncio.gather(*tasks)
        except BaseException as e:
            failed_event.set()
            for t in tasks:
                if not t.done():
                    t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if isinstance(e, asyncio.CancelledError):
                self._raise_for_control_state("chunked upload")
            raise

    async def resolve_entity(self, target: Any) -> Any:
        if not target:
            raise ValueError("Target destination chat is required.")

        target_str = str(target).strip()

        if target_str in ("me", "self", "Saved Messages"):
            return await self.client.get_input_entity("me")

        # 1. Try direct integer ID (handles -100..., -1001234567890, or positive user IDs)
        try:
            int_id = int(target_str)
            return await self.client.get_input_entity(int_id)
        except (ValueError, TypeError):
            pass

        # 2. Try direct Telegram username if it starts with @ or is a single word
        if target_str.startswith("@") or (" " not in target_str and target_str.isalnum()):
            try:
                return await self.client.get_input_entity(target_str)
            except Exception:
                pass

        # 3. Match against known bot dialogs / groups by title
        try:
            from .telegram_client import telegram_manager
            dialogs = await telegram_manager.get_dialogs()
            norm_target = "".join(c for c in target_str.lower() if c.isalnum())
            for d in dialogs:
                d_title = d.get("title", "")
                norm_title = "".join(c for c in d_title.lower() if c.isalnum())
                if norm_target and (norm_target in norm_title or norm_title in norm_target):
                    logger.info(f"Resolved target title '{target_str}' to dialog ID {d['id']} ('{d_title}')")
                    return await self.client.get_input_entity(int(d["id"]))
        except Exception as e:
            logger.warning(f"Dialog title matching failed for '{target_str}': {e}")

        # 4. Final fallback to Telethon entity resolver
        return await self.client.get_input_entity(target_str)
