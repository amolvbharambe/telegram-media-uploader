import unittest
import os
import subprocess
from pathlib import Path

from app import media_utils
from app.config import DATA_DIR
from telethon.tl import types

class TestMediaStreaming(unittest.TestCase):
    def setUp(self):
        self.test_mp4 = DATA_DIR / "stream_test.mp4"
        # Generate a real valid 2-second test MP4 using ffmpeg if available
        if media_utils.FFMPEG_PATH:
            cmd = [
                media_utils.FFMPEG_PATH,
                "-y",
                "-f", "lavfi",
                "-i", "testsrc=duration=2:size=320x240:rate=30",
                "-pix_fmt", "yuv420p",
                str(self.test_mp4)
            ]
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def tearDown(self):
        if self.test_mp4.exists():
            try:
                self.test_mp4.unlink()
            except Exception:
                pass

    def test_detect_media_type(self):
        self.assertEqual(media_utils.detect_media_type("movie.mp4"), "video")
        self.assertEqual(media_utils.detect_media_type("clip.mkv"), "video")
        self.assertEqual(media_utils.detect_media_type("track.mp3"), "audio")
        self.assertEqual(media_utils.detect_media_type("photo.jpg"), "photo")
        self.assertEqual(media_utils.detect_media_type("document.pdf"), "document")

    def test_streamable_attributes_generation(self):
        if not self.test_mp4.exists():
            self.skipTest("ffmpeg not available for generating test video")

        attrs, thumb_path, supports_streaming, mime_type = (
            media_utils.prepare_telegram_media_attributes(
                file_path=str(self.test_mp4),
                file_name="stream_test.mp4",
                media_type="video",
                force_document=False
            )
        )

        self.assertTrue(supports_streaming)
        self.assertEqual(mime_type, "video/mp4")

        # Check that DocumentAttributeVideo is present with supports_streaming=True
        video_attrs = [a for a in attrs if isinstance(a, types.DocumentAttributeVideo)]
        self.assertEqual(len(video_attrs), 1)
        self.assertTrue(video_attrs[0].supports_streaming)
        self.assertGreaterEqual(video_attrs[0].duration, 1)
        self.assertGreaterEqual(video_attrs[0].w, 100)
        self.assertGreaterEqual(video_attrs[0].h, 100)

        # Check thumbnail
        if thumb_path:
            self.assertTrue(os.path.exists(thumb_path))

    def test_image_exact_size_attributes(self):
        from PIL import Image
        test_img = DATA_DIR / "sample_img.jpg"
        img = Image.new("RGB", (800, 600), color=(120, 80, 200))
        img.save(str(test_img), format="JPEG")

        try:
            attrs, thumb_path, supports_streaming, mime_type = (
                media_utils.prepare_telegram_media_attributes(
                    file_path=str(test_img),
                    file_name="sample_img.jpg",
                    media_type="photo",
                    force_document=False
                )
            )

            self.assertEqual(mime_type, "image/jpeg")
            img_attrs = [a for a in attrs if isinstance(a, types.DocumentAttributeImageSize)]
            self.assertEqual(len(img_attrs), 1)
            self.assertEqual(img_attrs[0].w, 800)
            self.assertEqual(img_attrs[0].h, 600)
            if thumb_path:
                self.assertTrue(os.path.exists(thumb_path))
        finally:
            if test_img.exists():
                test_img.unlink()

if __name__ == "__main__":
    unittest.main()
