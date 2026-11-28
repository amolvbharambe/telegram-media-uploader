import os
import sys
import time
import asyncio
import unittest
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db
from app.config import DATA_DIR
from app.resumable_uploader import ResumableUploader
from telethon.tl.types import InputFile, InputFileBig

class MockTelegramMessage:
    def __init__(self, msg_id=9999):
        self.id = msg_id

class MockTelegramClient:
    """Mock TelegramClient for testing chunk uploads and resumption."""
    def __init__(self, fail_on_part: int = -1):
        self.fail_on_part = fail_on_part
        self.uploaded_parts = []
        self.sent_files = []

    def is_connected(self):
        return True

    async def is_user_authorized(self):
        return True

    async def __call__(self, request):
        # request is SaveBigFilePartRequest or SaveFilePartRequest
        part_index = request.file_part
        if part_index == self.fail_on_part:
            raise ConnectionError(f"Simulated network drop on part {part_index}!")
        self.uploaded_parts.append(part_index)
        return True

    async def get_input_entity(self, entity):
        return entity

    async def send_file(self, entity, file, **kwargs):
        self.sent_files.append((entity, file, kwargs))
        return MockTelegramMessage(msg_id=777)

class TestResumableUploader(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Use a fresh test db
        await db.init_db()
        self.test_file = DATA_DIR / "test_sample_media.dat"
        # 1.5 MB file (3 parts with 512KB chunk size)
        self.test_size = int(1.5 * 1024 * 1024)
        with open(self.test_file, "wb") as f:
            f.write(os.urandom(self.test_size))

    async def asyncTearDown(self):
        if self.test_file.exists():
            self.test_file.unlink()

    async def test_resumable_upload_after_failure(self):
        """
        Verify that when an upload fails mid-way, already uploaded parts
        remain saved in SQLite and the subsequent resume only uploads missing parts.
        """
        chunk_size = 512 * 1024
        total_parts = (self.test_size + chunk_size - 1) // chunk_size
        self.assertEqual(total_parts, 3)

        # Create task in DB
        task_id = await db.create_task(
            file_path=str(self.test_file),
            file_name=self.test_file.name,
            file_size=self.test_size,
            file_id=1234567890123456,
            target_chat="@testchannel",
            chunk_size=chunk_size,
            total_parts=total_parts
        )

        # 1. First run: Mock client simulates network drop on part 1
        mock_client_1 = MockTelegramClient(fail_on_part=1)
        uploader_1 = ResumableUploader(
            client=mock_client_1,
            task_id=task_id,
            max_concurrent=1  # sequential for strict part checking
        )

        with self.assertRaises(ConnectionError):
            await uploader_1.upload()

        # Part 0 should have uploaded successfully
        completed_parts = await db.get_completed_parts(task_id)
        self.assertIn(0, completed_parts)
        self.assertNotIn(1, completed_parts)
        self.assertNotIn(2, completed_parts)
        self.assertEqual(mock_client_1.uploaded_parts, [0])

        # 2. Second run: Resume! Mock client has no failure
        mock_client_2 = MockTelegramClient(fail_on_part=-1)
        uploader_2 = ResumableUploader(
            client=mock_client_2,
            task_id=task_id,
            max_concurrent=1
        )

        msg_id = await uploader_2.upload()
        self.assertEqual(msg_id, 777)

        # Crucial check: mock_client_2 should NOT have uploaded part 0 again!
        # It should ONLY have uploaded part 1 and part 2!
        self.assertEqual(sorted(mock_client_2.uploaded_parts), [1, 2])

        # Verify all 3 parts are recorded in database
        final_parts = await db.get_completed_parts(task_id)
        self.assertEqual(final_parts, {0, 1, 2})

        # Verify task is completed
        task = await db.get_task(task_id)
        self.assertEqual(task["status"], "completed")
        self.assertEqual(task["message_id"], 777)
        self.assertEqual(task["bytes_uploaded"], self.test_size)

    async def test_skip_all_if_already_completed(self):
        """
        If all parts are already uploaded, uploader should jump directly to send_file.
        """
        chunk_size = 512 * 1024
        total_parts = (self.test_size + chunk_size - 1) // chunk_size

        task_id = await db.create_task(
            file_path=str(self.test_file),
            file_name=self.test_file.name,
            file_size=self.test_size,
            file_id=9876543210987654,
            target_chat="@mychannel",
            chunk_size=chunk_size,
            total_parts=total_parts
        )

        # Mark all parts as pre-completed in DB
        for p in range(total_parts):
            await db.record_part_upload(task_id, p, chunk_size, time.time())

        mock_client = MockTelegramClient()
        uploader = ResumableUploader(client=mock_client, task_id=task_id)
        msg_id = await uploader.upload()

        self.assertEqual(msg_id, 777)
        # 0 chunks should have been uploaded to Telegram
        self.assertEqual(mock_client.uploaded_parts, [])
        self.assertEqual(len(mock_client.sent_files), 1)

    async def test_resolve_entity_matching(self):
        """Test entity resolution with integer, username, and dialog title matching."""
        mock_client = MockTelegramClient()
        uploader = ResumableUploader(client=mock_client, task_id=999)

        # 1. 'me'
        self.assertEqual(await uploader.resolve_entity("me"), "me")
        # 2. Integer ID
        self.assertEqual(await uploader.resolve_entity("-1001234567890"), -1001234567890)
        self.assertEqual(await uploader.resolve_entity(123456), 123456)
        # 3. Username
        self.assertEqual(await uploader.resolve_entity("@mychannel"), "@mychannel")

if __name__ == "__main__":
    unittest.main()
