import os
import sys
import time
import asyncio
import unittest
from pathlib import Path
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db
from app.config import DATA_DIR
from app.main import app
from app.queue_manager import queue_manager

class TestApiAndQueue(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await db.init_db()
        self.client = TestClient(app)
        self.sample_file = DATA_DIR / "sample_api_test.mp4"
        with open(self.sample_file, "wb") as f:
            f.write(b"0" * (1024 * 1024)) # 1MB

    async def asyncTearDown(self):
        if self.sample_file.exists():
            self.sample_file.unlink()

    def test_status_endpoint(self):
        response = self.client.get("/api/status")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("auth", data)
        self.assertIn("stats", data)
        self.assertIn("config", data)

    def test_upload_local_and_task_lifecycle(self):
        # 1. Queue local file
        response = self.client.post("/api/upload-local", json={
            "file_path": str(self.sample_file),
            "target_chat": "@samplechat",
            "caption": "Test video upload",
            "media_type": "video",
            "force_document": False
        })
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["success"])
        task_id = data["task_id"]

        # 2. Get task details
        res_task = self.client.get(f"/api/tasks/{task_id}")
        self.assertEqual(res_task.status_code, 200)
        task_data = res_task.json()["task"]
        self.assertEqual(task_data["file_name"], self.sample_file.name)
        self.assertEqual(task_data["target_chat"], "@samplechat")

        # 3. Pause task
        res_pause = self.client.post(f"/api/tasks/{task_id}/pause")
        self.assertEqual(res_pause.status_code, 200)

        # 4. Resume task
        res_resume = self.client.post(f"/api/tasks/{task_id}/resume")
        self.assertEqual(res_resume.status_code, 200)

        # 5. Delete task
        res_delete = self.client.delete(f"/api/tasks/{task_id}")
        self.assertEqual(res_delete.status_code, 200)
        self.assertTrue(res_delete.json()["success"])

    def test_settings_endpoint(self):
        response = self.client.get("/api/settings")
        self.assertEqual(response.status_code, 200)

        res_update = self.client.post("/api/settings", json={
            "chunk_size_kb": 256,
            "max_concurrent_chunks": 4,
            "max_retries": 15
        })
        self.assertEqual(res_update.status_code, 200)
        self.assertEqual(res_update.json()["settings"]["chunk_size_kb"], 256)
        self.assertEqual(res_update.json()["settings"]["max_concurrent_chunks"], 4)

    def test_scan_folder_and_batch_upload(self):
        # Create a test directory with 3 files
        test_dir = DATA_DIR / "test_batch_folder"
        test_dir.mkdir(parents=True, exist_ok=True)
        try:
            for i in range(3):
                with open(test_dir / f"file_{i}.txt", "w") as f:
                    f.write(f"Sample content {i}")

            # 1. Test scan folder endpoint
            res_scan = self.client.post("/api/scan-folder", json={
                "folder_path": str(test_dir),
                "recursive": False
            })
            self.assertEqual(res_scan.status_code, 200)
            data_scan = res_scan.json()
            self.assertTrue(data_scan["is_dir"])
            self.assertEqual(data_scan["count"], 3)

            # 2. Test batch queueing of the directory
            res_upload = self.client.post("/api/upload-local", json={
                "file_path": str(test_dir),
                "target_chat": "@batchtest",
                "recursive": False
            })
            self.assertEqual(res_upload.status_code, 200)
            data_upload = res_upload.json()
            self.assertTrue(data_upload["success"])
            self.assertEqual(data_upload["queued_count"], 3)
            self.assertEqual(len(data_upload["tasks"]), 3)

        finally:
            # Clean up
            import shutil
            if test_dir.exists():
                shutil.rmtree(test_dir)

    def test_multi_file_browser_upload(self):
        # Test posting multiple files simultaneously
        files = [
            ("files", ("video1.mp4", b"video_data_1", "video/mp4")),
            ("files", ("photo2.png", b"photo_data_2", "image/png"))
        ]
        data = {
            "target_chat": "@multifiletest",
            "caption": "Multi batch",
            "media_type": "auto",
            "force_document": "false"
        }
        res = self.client.post("/api/upload", data=data, files=files)
        self.assertEqual(res.status_code, 200)
        json_data = res.json()
        self.assertTrue(json_data["success"])
        self.assertEqual(json_data["queued_count"], 2)
        self.assertEqual(len(json_data["tasks"]), 2)

if __name__ == "__main__":
    unittest.main()
