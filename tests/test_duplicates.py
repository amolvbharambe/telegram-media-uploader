import os
import sys
import unittest
from pathlib import Path
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db
from app.config import DATA_DIR
from app.main import app

class TestDuplicateChecking(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await db.init_db()
        self.client = TestClient(app)
        self.target_chat = "-1001234567890"
        self.test_file = DATA_DIR / "test_dup_sample.mp4"
        with open(self.test_file, "wb") as f:
            f.write(b"DUPLICATE_TEST_DATA_BYTES")

    async def asyncTearDown(self):
        if self.test_file.exists():
            self.test_file.unlink()

    async def test_check_duplicates_flow(self):
        # 1. Check with a file that is not in DB
        res = self.client.post("/api/check-duplicates", json={
            "target_chat": self.target_chat,
            "files": [{"name": "brand_new_video_12345.mp4", "size": 12345}]
        })
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["checked_files_count"], 1)
        self.assertEqual(data["duplicate_count"], 0)
        self.assertEqual(data["file_matches"][0]["status"], "unique")

        # 2. Insert a simulated completed task into DB
        task_id = await db.create_task(
            file_path=str(self.test_file),
            file_name="already_uploaded_video.mp4",
            file_size=len(b"DUPLICATE_TEST_DATA_BYTES"),
            file_id=999888777,
            target_chat=self.target_chat,
            chat_title="Test Media Group",
            caption="Test",
            media_type="video",
            force_document=False,
            chunk_size=512 * 1024,
            total_parts=1,
            max_retries=3
        )
        await db.update_task_status(task_id, "completed", message_id=888)

        # 3. Check duplicate matching for the exact file
        res2 = self.client.post("/api/check-duplicates", json={
            "target_chat": self.target_chat,
            "files": [{"name": "already_uploaded_video.mp4", "size": len(b"DUPLICATE_TEST_DATA_BYTES")}]
        })
        self.assertEqual(res2.status_code, 200)
        data2 = res2.json()
        self.assertEqual(data2["duplicate_count"], 1)
        self.assertEqual(data2["file_matches"][0]["status"], "exact_duplicate")
        self.assertEqual(data2["file_matches"][0]["matched_with"]["message_id"], 888)

        # 4. Test upload-local with skip_duplicates=True
        res3 = self.client.post("/api/upload-local", json={
            "file_path": str(self.test_file),
            "target_chat": self.target_chat,
            "skip_duplicates": True
        })
        self.assertEqual(res3.status_code, 200)
        # File name on disk is test_dup_sample.mp4, which is not already_uploaded_video.mp4
        self.assertEqual(res3.json()["queued_count"], 1)

        # Clean up created task
        await db.delete_task(task_id)
        await db.delete_task(res3.json()["tasks"][0]["id"])

if __name__ == "__main__":
    unittest.main()
