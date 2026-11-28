import os
import shutil
import logging
import subprocess
import json
from pathlib import Path
from typing import Optional, Tuple, List, Dict, Any

from telethon.tl import types
from .config import THUMBS_DIR

logger = logging.getLogger("media_utils")
THUMBS_DIR.mkdir(parents=True, exist_ok=True)

FFMPEG_PATH = shutil.which("ffmpeg") or (
    "/opt/homebrew/bin/ffmpeg" if os.path.exists("/opt/homebrew/bin/ffmpeg") else None
)
FFPROBE_PATH = shutil.which("ffprobe") or (
    "/opt/homebrew/bin/ffprobe" if os.path.exists("/opt/homebrew/bin/ffprobe") else None
)

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".flv", ".m4v", ".3gp", ".wmv", ".ts"}
AUDIO_EXTENSIONS = {".mp3", ".m4a", ".flac", ".wav", ".ogg", ".opus", ".aac", ".wma"}
PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}

def detect_media_type(filename: str, explicit_type: str = "auto") -> str:
    """Detects whether a file should be treated as video, audio, photo, or document."""
    if explicit_type and explicit_type != "auto":
        return explicit_type

    ext = Path(filename).suffix.lower()
    if ext in VIDEO_EXTENSIONS:
        return "video"
    if ext in AUDIO_EXTENSIONS:
        return "audio"
    if ext in PHOTO_EXTENSIONS:
        return "photo"
    return "document"

def check_mp4_faststart(file_path: str) -> bool:
    """
    Checks if an MP4/MOV file has the 'moov' atom before the 'mdat' atom.
    If 'moov' is before 'mdat', streaming can begin immediately.
    """
    try:
        with open(file_path, "rb") as f:
            while True:
                header = f.read(8)
                if len(header) < 8:
                    return False
                size = int.from_bytes(header[:4], "big")
                tag = header[4:8]
                if tag == b"moov":
                    return True
                if tag == b"mdat":
                    return False
                if size == 1:
                    size = int.from_bytes(f.read(8), "big")
                    f.seek(size - 16, os.SEEK_CUR)
                elif size > 8:
                    f.seek(size - 8, os.SEEK_CUR)
                else:
                    return False
    except Exception as e:
        logger.debug(f"Faststart check error: {e}")
        return False

def ensure_mp4_faststart(file_path: str) -> str:
    """
    If the file is MP4/MOV and not already faststart, remuxes moov atom to the beginning
    with ffmpeg -movflags +faststart without re-encoding (instant streamable).
    """
    ext = Path(file_path).suffix.lower()
    if ext not in (".mp4", ".mov", ".m4v") or not FFMPEG_PATH:
        return file_path

    if check_mp4_faststart(file_path):
        return file_path

    logger.info(f"Optimizing video for streaming (moving index to start of file): {file_path}")
    temp_output = f"{file_path}.faststart{ext}"
    try:
        cmd = [
            FFMPEG_PATH,
            "-y",
            "-i", file_path,
            "-c", "copy",
            "-movflags", "+faststart",
            temp_output
        ]
        res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=120)
        if res.returncode == 0 and os.path.exists(temp_output) and os.path.getsize(temp_output) > 0:
            os.replace(temp_output, file_path)
            logger.info("Faststart optimization complete.")
            return file_path
    except Exception as e:
        logger.warning(f"Failed to apply faststart to {file_path}: {e}")
    finally:
        if os.path.exists(temp_output):
            try:
                os.unlink(temp_output)
            except Exception:
                pass

    return file_path

def get_video_metadata(file_path: str) -> Dict[str, Any]:
    """Extracts duration, width, and height of a video file."""
    # 1. Try ffprobe first
    if FFPROBE_PATH and os.path.exists(file_path):
        try:
            cmd = [
                FFPROBE_PATH,
                "-v", "error",
                "-show_entries", "stream=width,height,duration:format=duration",
                "-of", "json",
                file_path
            ]
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10)
            if res.returncode == 0:
                data = json.loads(res.stdout)
                streams = data.get("streams", [])
                format_info = data.get("format", {})
                duration = 0
                width = 0
                height = 0

                for s in streams:
                    if "width" in s and "height" in s and width == 0:
                        width = int(s["width"])
                        height = int(s["height"])
                    if "duration" in s and s["duration"]:
                        try:
                            duration = int(float(s["duration"]))
                        except (ValueError, TypeError):
                            pass

                if duration == 0 and format_info.get("duration"):
                    try:
                        duration = int(float(format_info["duration"]))
                    except (ValueError, TypeError):
                        pass

                if width > 0 and height > 0:
                    return {"width": width, "height": height, "duration": max(1, duration)}
        except Exception as e:
            logger.debug(f"ffprobe extraction error: {e}")

    # 2. Try hachoir
    try:
        from hachoir.parser import createParser
        from hachoir.metadata import extractMetadata

        parser = createParser(file_path)
        if parser:
            with parser:
                metadata = extractMetadata(parser)
                if metadata:
                    w = metadata.get("width") if metadata.has("width") else 1280
                    h = metadata.get("height") if metadata.has("height") else 720
                    duration = int(metadata.get("duration").seconds) if metadata.has("duration") else 1
                    return {"width": int(w), "height": int(h), "duration": max(1, duration)}
    except Exception as e:
        logger.debug(f"hachoir extraction error: {e}")

    return {"width": 1280, "height": 720, "duration": 1}

def get_audio_metadata(file_path: str) -> Dict[str, Any]:
    """Extracts duration, performer/artist, and title from an audio file."""
    title = None
    performer = None
    duration = 0

    try:
        from hachoir.parser import createParser
        from hachoir.metadata import extractMetadata

        parser = createParser(file_path)
        if parser:
            with parser:
                metadata = extractMetadata(parser)
                if metadata:
                    if metadata.has("title"):
                        title = str(metadata.get("title"))
                    if metadata.has("author"):
                        performer = str(metadata.get("author"))
                    elif metadata.has("artist"):
                        performer = str(metadata.get("artist"))
                    if metadata.has("duration"):
                        duration = int(metadata.get("duration").seconds)
    except Exception as e:
        logger.debug(f"Audio metadata extraction error: {e}")

    return {
        "title": title or Path(file_path).stem,
        "performer": performer or "",
        "duration": max(0, duration)
    }

def generate_video_thumbnail(file_path: str) -> Optional[str]:
    """Generates a JPEG thumbnail from the 1-second mark of a video."""
    if not FFMPEG_PATH or not os.path.exists(file_path):
        return None

    safe_name = f"thumb_{Path(file_path).stem}_{os.path.getsize(file_path)}.jpg"
    thumb_path = THUMBS_DIR / safe_name
    if thumb_path.exists() and thumb_path.stat().st_size > 0:
        return str(thumb_path)

    try:
        cmd = [
            FFMPEG_PATH,
            "-y",
            "-ss", "00:00:01",
            "-i", file_path,
            "-vframes", "1",
            "-q:v", "3",
            "-vf", "scale=320:-1",
            str(thumb_path)
        ]
        res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=15)
        if res.returncode == 0 and thumb_path.exists() and thumb_path.stat().st_size > 0:
            return str(thumb_path)
    except Exception as e:
        logger.debug(f"Thumbnail generation error: {e}")

    return None

def generate_image_thumbnail(file_path: str) -> Optional[str]:
    """Generates a JPEG thumbnail preview for an image using PIL."""
    if not os.path.exists(file_path):
        return None

    safe_name = f"thumb_img_{Path(file_path).stem}_{os.path.getsize(file_path)}.jpg"
    thumb_path = THUMBS_DIR / safe_name
    if thumb_path.exists() and thumb_path.stat().st_size > 0:
        return str(thumb_path)

    try:
        from PIL import Image
        with Image.open(file_path) as img:
            img = img.convert("RGB")
            img.thumbnail((320, 320))
            img.save(str(thumb_path), format="JPEG", quality=85)
            return str(thumb_path)
    except Exception as e:
        logger.debug(f"Image thumbnail generation error: {e}")
        return None

def get_image_metadata(file_path: str) -> Dict[str, int]:
    """Extracts width and height of an image."""
    try:
        from PIL import Image
        with Image.open(file_path) as img:
            return {"width": img.width, "height": img.height}
    except Exception as e:
        logger.debug(f"Image metadata extraction error: {e}")
        return {"width": 1920, "height": 1080}

def prepare_telegram_media_attributes(
    file_path: str,
    file_name: str,
    media_type: str = "auto",
    force_document: bool = False
) -> Tuple[List[Any], Optional[str], bool, str]:
    """
    Builds the exact Telegram DocumentAttributes for streamable media.
    Returns: (attributes_list, thumbnail_path, supports_streaming, mime_type)
    """
    m_type = detect_media_type(file_name, media_type)
    attributes = [types.DocumentAttributeFilename(file_name=file_name)]
    thumb_path = None
    supports_streaming = False
    mime_type = "application/octet-stream"

    if force_document or m_type == "document":
        return attributes, None, False, mime_type

    if m_type == "video":
        supports_streaming = True
        ext = Path(file_name).suffix.lower()
        if ext in (".mp4", ".mov", ".m4v"):
            mime_type = "video/mp4"
        elif ext == ".webm":
            mime_type = "video/webm"
        elif ext == ".mkv":
            mime_type = "video/x-matroska"
        else:
            mime_type = "video/mp4"
        meta = get_video_metadata(file_path)
        thumb_path = generate_video_thumbnail(file_path)

        video_attr = types.DocumentAttributeVideo(
            duration=meta["duration"],
            w=meta["width"],
            h=meta["height"],
            supports_streaming=True
        )
        attributes.append(video_attr)

    elif m_type == "audio":
        mime_type = "audio/mpeg" if Path(file_name).suffix.lower() == ".mp3" else "audio/mp4"
        meta = get_audio_metadata(file_path)
        audio_attr = types.DocumentAttributeAudio(
            duration=meta["duration"],
            title=meta["title"],
            performer=meta["performer"],
            voice=False
        )
        attributes.append(audio_attr)

    elif m_type == "photo":
        ext = Path(file_name).suffix.lower()
        if ext in (".jpg", ".jpeg"):
            mime_type = "image/jpeg"
        elif ext == ".png":
            mime_type = "image/png"
        elif ext == ".webp":
            mime_type = "image/webp"
        elif ext == ".gif":
            mime_type = "image/gif"
        else:
            mime_type = "image/jpeg"

        meta = get_image_metadata(file_path)
        thumb_path = generate_image_thumbnail(file_path)
        img_attr = types.DocumentAttributeImageSize(
            w=meta["width"],
            h=meta["height"]
        )
        attributes.append(img_attr)

    return attributes, thumb_path, supports_streaming, mime_type
