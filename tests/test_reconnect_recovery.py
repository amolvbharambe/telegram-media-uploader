import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db
from app.queue_manager import QueueManager
from app.resumable_uploader import (
    PartsExpiredException,
    ResumableUploader,
    UploadCancelledException,
    UploadPausedException,
)
from telethon import errors


class _DummyMsg:
    def __init__(self, msg_id=4242):
        self.id = msg_id


class TestDisconnectAndRecovery(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        self._tmp_path = Path(self._tmp_dir.name)
        self._orig_db_path = db.DB_PATH
        self._orig_db_init = db._db_initialized
        db.DB_PATH = self._tmp_path / "test_recovery.db"
        db._db_initialized = False
        await db.init_db()

        self.sample_file = self._tmp_path / "sample.bin"
        self.chunk_size = 512 * 1024
        self.sample_size = self.chunk_size * 3  # 3 parts
        self.sample_file.write_bytes(b"A" * self.sample_size)

    async def asyncTearDown(self):
        db.DB_PATH = self._orig_db_path
        db._db_initialized = self._orig_db_init
        self._tmp_dir.cleanup()

    async def _create_test_task(self) -> int:
        return await db.create_task(
            file_path=str(self.sample_file),
            file_name=self.sample_file.name,
            file_size=self.sample_size,
            file_id=987654321,
            target_chat="-1001234567890",
            chunk_size=self.chunk_size,
            total_parts=3,
        )

    async def test_telethon_disconnect_cancelled_error_becomes_connection_error(self):
        """Simulates Telethon MTProtoSender._disconnect() cancelling an in-flight part future."""
        task_id = await self._create_test_task()
        uploaded = []

        class CancelOnPart1Client:
            def is_connected(self):
                return True

            async def __call__(self, req):
                if req.file_part == 1:
                    raise asyncio.CancelledError()
                uploaded.append(req.file_part)
                return True

            async def get_input_entity(self, entity):
                return entity

            async def send_file(self, **kwargs):
                return _DummyMsg(111)

        uploader = ResumableUploader(
            client=CancelOnPart1Client(),
            task_id=task_id,
            max_concurrent=1,
        )
        with self.assertRaises(ConnectionError):
            await uploader.upload()

        parts = await db.get_completed_parts(task_id)
        self.assertEqual(parts, {0})
        self.assertEqual(uploaded, [0])

    async def test_hung_socket_times_out_cleanly(self):
        """Simulates a silent TCP blackhole during internet disconnect/reconnect."""
        task_id = await self._create_test_task()

        class HangingClient:
            def is_connected(self):
                return True

            async def __call__(self, req):
                await asyncio.sleep(10)
                return True

            async def get_input_entity(self, entity):
                return entity

        uploader = ResumableUploader(
            client=HangingClient(),
            task_id=task_id,
            max_concurrent=1,
        )
        uploader.PART_UPLOAD_TIMEOUT = 0.05

        with self.assertRaises(ConnectionError):
            await asyncio.wait_for(uploader.upload(), timeout=1.0)

    async def test_pause_interrupts_hung_rpc_immediately(self):
        """Clicking Pause during a hung chunk RPC must yield UploadPausedException immediately."""
        task_id = await self._create_test_task()
        pause_event = asyncio.Event()

        class SlowClient:
            async def __call__(self, req):
                await asyncio.sleep(5)
                return True

        uploader = ResumableUploader(
            client=SlowClient(),
            task_id=task_id,
            pause_event=pause_event,
            max_concurrent=1,
        )

        upload_task = asyncio.create_task(uploader.upload())
        await asyncio.sleep(0.05)
        pause_event.set()
        upload_task.cancel()

        with self.assertRaises(UploadPausedException):
            await asyncio.wait_for(upload_task, timeout=1.0)

    async def test_queue_manager_recovers_after_disconnect_and_keeps_worker_alive(self):
        """Full end-to-end test: disconnect CancelledError -> reconnect -> automatic resume."""
        task_id = await self._create_test_task()
        attempt = {"count": 0}
        sent_messages = []

        class FlakyClient:
            def is_connected(self):
                return True

            async def is_user_authorized(self):
                return True

            async def __call__(self, req):
                if attempt["count"] == 0 and req.file_part == 1:
                    attempt["count"] += 1
                    raise asyncio.CancelledError()
                return True

            async def get_input_entity(self, entity):
                return entity

            async def send_file(self, **kwargs):
                sent_messages.append(kwargs)
                return _DummyMsg(555)

        flaky_client = FlakyClient()
        qm = QueueManager()

        with (
            patch("app.queue_manager.load_config", return_value={
                "retry_base_delay": 0.05,
                "retry_max_delay": 0.1,
                "max_concurrent_chunks": 1,
                "auto_delete_local_on_finish": False,
            }),
            patch("app.queue_manager.telegram_manager.get_client", new=AsyncMock(return_value=flaky_client)),
            patch("app.queue_manager.telegram_manager.reconnect", new=AsyncMock(return_value=flaky_client)) as mock_reconnect,
        ):
            await qm.start()
            try:
                # Poll until completed (should auto-recover in < 2.5s)
                for _ in range(50):
                    t = await db.get_task(task_id)
                    if t["status"] == "completed":
                        break
                    await asyncio.sleep(0.1)

                final_task = await db.get_task(task_id)
                self.assertEqual(final_task["status"], "completed")
                self.assertEqual(final_task["message_id"], 555)
                self.assertGreaterEqual(mock_reconnect.await_count, 1)
                self.assertFalse(qm._worker_task.done())
            finally:
                await qm.stop()

        # Stopping the server must never overwrite a completed/active task with 'cancelled'
        after_stop = await db.get_task(task_id)
        self.assertEqual(after_stop["status"], "completed")

    async def test_single_missing_part_repaired_on_send_media(self):
        """If Telegram raises FilePartMissingError on SendMediaRequest, repair just that part inline."""
        task_id = await self._create_test_task()
        for p in range(3):
            await db.record_part_upload(task_id, p, self.chunk_size, 1.0)

        reuploaded = []
        send_calls = {"n": 0}

        class MissingPart1Client:
            async def __call__(self, req):
                reuploaded.append(req.file_part)
                return True

            async def get_input_entity(self, entity):
                return entity

            async def send_file(self, **kwargs):
                send_calls["n"] += 1
                if send_calls["n"] == 1:
                    raise errors.FilePartMissingError(request=None, capture=1)
                return _DummyMsg(7777)

        uploader = ResumableUploader(client=MissingPart1Client(), task_id=task_id, max_concurrent=1)
        msg_id = await uploader.upload()

        self.assertEqual(msg_id, 7777)
        self.assertEqual(reuploaded, [1])
        self.assertEqual(send_calls["n"], 2)
        t = await db.get_task(task_id)
        self.assertEqual(t["status"], "completed")

    async def test_repeated_missing_parts_trigger_parts_expired(self):
        """If Telegram rejects >3 parts on SendMediaRequest, raise PartsExpiredException for fresh file_id."""
        task_id = await self._create_test_task()
        for p in range(3):
            await db.record_part_upload(task_id, p, self.chunk_size, 1.0)

        class AllExpiredClient:
            def __init__(self):
                self.missing_seq = iter([0, 1, 2, 0])

            async def __call__(self, req):
                return True

            async def get_input_entity(self, entity):
                return entity

            async def send_file(self, **kwargs):
                idx = next(self.missing_seq, 0)
                raise errors.FilePartMissingError(request=None, capture=idx)

        uploader = ResumableUploader(client=AllExpiredClient(), task_id=task_id, max_concurrent=1)
        with self.assertRaises(PartsExpiredException):
            await uploader.upload()


if __name__ == "__main__":
    unittest.main()
