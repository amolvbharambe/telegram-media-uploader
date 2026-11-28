import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db, main as main_mod
from app.main import app


class TestUploadCacheManagement(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        self._tmp_path = Path(self._tmp_dir.name)
        self._uploads = self._tmp_path / "uploads"
        self._thumbs = self._tmp_path / "thumbs"
        self._external = self._tmp_path / "external_source"
        self._uploads.mkdir()
        self._thumbs.mkdir()
        self._external.mkdir()

        self._orig_db_path = db.DB_PATH
        self._orig_db_init = db._db_initialized
        self._orig_uploads = main_mod.UPLOADS_DIR
        self._orig_thumbs = main_mod.THUMBS_DIR

        db.DB_PATH = self._tmp_path / "test_cache.db"
        db._db_initialized = False
        main_mod.UPLOADS_DIR = self._uploads
        main_mod.THUMBS_DIR = self._thumbs
        await db.init_db()

        self.client = TestClient(app)

    async def asyncTearDown(self):
        db.DB_PATH = self._orig_db_path
        db._db_initialized = self._orig_db_init
        main_mod.UPLOADS_DIR = self._orig_uploads
        main_mod.THUMBS_DIR = self._orig_thumbs
        self._tmp_dir.cleanup()

    async def test_clear_cache_preserves_active_and_external_files(self):
        # 1. Create active queued staged file (MUST BE PROTECTED)
        active_staged = self._uploads / "111_active_video.mp4"
        active_staged.write_bytes(b"A" * 2048)
        t_active = await db.create_task(
            file_path=str(active_staged),
            file_name="active_video.mp4",
            file_size=2048,
            file_id=111,
            target_chat="-1001",
            chunk_size=1024,
            total_parts=2,
        )

        # 2. Create completed staged file + orphan + thumbnail (SAFE TO DELETE)
        done_staged = self._uploads / "222_done_video.mp4"
        done_staged.write_bytes(b"B" * 4096)
        t_done = await db.create_task(
            file_path=str(done_staged),
            file_name="done_video.mp4",
            file_size=4096,
            file_id=222,
            target_chat="-1001",
            chunk_size=1024,
            total_parts=4,
        )
        await db.update_task_status(t_done, status="completed", message_id=99)

        orphan_staged = self._uploads / "orphan.tmp"
        orphan_staged.write_bytes(b"C" * 1024)

        thumb_file = self._thumbs / "thumb_222.jpg"
        thumb_file.write_bytes(b"T" * 512)
        import os
        # Backdate thumb older than active-upload 120s grace window
        old_ts = 1_600_000_000
        os.utime(thumb_file, (old_ts, old_ts))

        # 3. Create external source file referenced by active task outside UPLOADS_DIR
        external_src = self._external / "original_camera.mov"
        external_src.write_bytes(b"E" * 8192)
        await db.create_task(
            file_path=str(external_src),
            file_name="original_camera.mov",
            file_size=8192,
            file_id=333,
            target_chat="-1001",
            chunk_size=1024,
            total_parts=8,
        )

        # Check stats endpoint
        res_stats = self.client.get("/api/cache/stats")
        self.assertEqual(res_stats.status_code, 200)
        stats = res_stats.json()
        self.assertEqual(stats["uploads_bytes"], 4096 + 1024)
        self.assertEqual(stats["uploads_count"], 2)
        self.assertEqual(stats["thumbs_bytes"], 512)
        self.assertEqual(stats["protected_bytes"], 2048)
        self.assertEqual(stats["protected_count"], 1)

        # Clear cache
        res_clear = self.client.post("/api/cache/clear", json={"include_thumbs": True})
        self.assertEqual(res_clear.status_code, 200)
        cleared = res_clear.json()
        self.assertTrue(cleared["success"])
        self.assertEqual(cleared["deleted_files_count"], 3)
        self.assertEqual(cleared["freed_bytes"], 4096 + 1024 + 512)
        self.assertEqual(cleared["skipped_protected_count"], 1)

        # Active staged file and external original MUST survive; completed + orphan + old thumb MUST be gone
        self.assertTrue(active_staged.exists())
        self.assertTrue(external_src.exists())
        self.assertFalse(done_staged.exists())
        self.assertFalse(orphan_staged.exists())
        self.assertFalse(thumb_file.exists())

        # Completed DB record must still exist so duplicate scanning stays accurate
        completed_history = await db.get_completed_tasks_by_chat("-1001")
        self.assertEqual(len(completed_history), 1)
        self.assertEqual(completed_history[0]["id"], t_done)


if __name__ == "__main__":
    unittest.main()
