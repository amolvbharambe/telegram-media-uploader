import os
import shutil
import asyncio
import logging
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Optional, List, Dict, Any

from fastapi import (
    FastAPI, UploadFile, File, Form, HTTPException,
    WebSocket, WebSocketDisconnect, Query, BackgroundTasks
)
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from telethon import helpers

from .config import UPLOADS_DIR, THUMBS_DIR, load_config, save_config, BASE_DIR
from . import db
from .telegram_client import telegram_manager
from .queue_manager import queue_manager

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("api")

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    await db.init_db()
    try:
        await telegram_manager.initialize()
    except Exception as e:
        logger.warning(f"Initial Telegram connection failed: {e}")
    await queue_manager.start()
    yield
    # Shutdown
    await queue_manager.stop()
    if telegram_manager.client:
        try:
            await telegram_manager.client.disconnect()
        except Exception:
            pass

app = FastAPI(title="Telegram Resumable Media Uploader", lifespan=lifespan)

# Static files directory
STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

@app.get("/")
async def root():
    return FileResponse(
        STATIC_DIR / "index.html",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache"}
    )

@app.api_route("/download/app.apk", methods=["GET", "HEAD"])
async def download_apk():
    apk_path = BASE_DIR / "telegram-uploader.apk"
    if not apk_path.exists():
        raise HTTPException(status_code=404, detail="APK not found")
    return FileResponse(
        path=str(apk_path),
        filename="TelegramUploader.apk",
        media_type="application/vnd.android.package-archive"
    )

# Schemas
class BotLoginRequest(BaseModel):
    bot_token: str
    api_id: Optional[int] = None
    api_hash: Optional[str] = None

class PhoneSendCodeRequest(BaseModel):
    api_id: int
    api_hash: str
    phone: str

class PhoneVerifyCodeRequest(BaseModel):
    code: str
    password: Optional[str] = None

class LocalFileUploadRequest(BaseModel):
    file_path: str
    target_chat: str
    chat_title: Optional[str] = None
    caption: Optional[str] = None
    media_type: str = "auto"
    force_document: bool = False
    recursive: bool = False
    skip_duplicates: bool = False

class ScanFolderRequest(BaseModel):
    folder_path: str
    recursive: bool = False

class SettingsUpdateRequest(BaseModel):
    chunk_size_kb: Optional[int] = None
    max_concurrent_chunks: Optional[int] = None
    max_retries: Optional[int] = None
    retry_base_delay: Optional[float] = None
    retry_max_delay: Optional[float] = None
    auto_delete_local_on_finish: Optional[bool] = None

class CheckDuplicatesRequest(BaseModel):
    target_chat: str
    files: Optional[List[Dict[str, Any]]] = None

class DeleteGroupMessageRequest(BaseModel):
    target_chat: str
    message_ids: List[int]

class UpgradeStreamableRequest(BaseModel):
    target_chat: str = ""
    limit: Optional[int] = 0
    task_id: Optional[int] = None
    delete_old: bool = True

class ClearCacheRequest(BaseModel):
    include_thumbs: bool = True

def _scan_cache_directory(directory: Path, protected_paths: set) -> Dict[str, Any]:
    """Scans a staging/cache directory separating safe-to-delete vs protected active files."""
    reclaimable = []
    protected = []
    reclaimable_bytes = 0
    protected_bytes = 0

    if not directory.exists():
        return {
            "reclaimable": reclaimable,
            "protected": protected,
            "reclaimable_bytes": 0,
            "protected_bytes": 0,
        }

    base_resolved = directory.resolve()
    for item in directory.rglob("*"):
        if not item.is_file():
            continue
        try:
            resolved = str(item.resolve())
            size = item.stat().st_size
        except OSError:
            continue
        if not resolved.startswith(str(base_resolved)):
            continue
        if resolved in protected_paths:
            protected.append(resolved)
            protected_bytes += size
        else:
            reclaimable.append(item)
            reclaimable_bytes += size

    return {
        "reclaimable": reclaimable,
        "protected": protected,
        "reclaimable_bytes": reclaimable_bytes,
        "protected_bytes": protected_bytes,
    }

# Routes
@app.get("/api/status")
async def get_status():
    auth_status = await telegram_manager.get_status()
    stats = await db.get_dashboard_stats()
    cfg = load_config()
    return {
        "auth": auth_status,
        "stats": stats,
        "config": {
            "api_id": bool(cfg.get("api_id")),
            "auth_mode": cfg.get("auth_mode", "bot"),
            "chunk_size_kb": cfg.get("chunk_size_kb", 512),
            "max_concurrent_chunks": cfg.get("max_concurrent_chunks", 3),
            "max_retries": cfg.get("max_retries", 10),
            "auto_delete_local_on_finish": cfg.get("auto_delete_local_on_finish", False)
        }
    }

@app.get("/api/dialogs")
async def get_dialogs(limit: int = 100):
    dialogs = await telegram_manager.get_dialogs(limit=limit)
    return {"dialogs": dialogs}

@app.post("/api/check-duplicates")
async def check_duplicates(req: CheckDuplicatesRequest):
    """
    Checks for duplicate files in the target Telegram group:
    1. Scans remote group messages & local DB history.
    2. Identifies duplicate pairs already in the group.
    3. Matches incoming files against known files to prevent re-uploading duplicates.
    """
    target_chat = req.target_chat.strip()
    if not target_chat:
        raise HTTPException(status_code=400, detail="target_chat is required")

    # 1. Fetch remote messages from Telegram group/channel
    remote_items = []
    error_notice = None
    try:
        remote_scan = await telegram_manager.scan_group_media(target_chat, limit=1000)
        remote_items = remote_scan.get("media_items", [])
        error_notice = remote_scan.get("error_notice")
    except Exception as e:
        logger.warning(f"Remote group scan skipped: {e}")
        error_notice = str(e)

    # 2. Fetch local database history of completed uploads for this chat
    db_items = await db.get_completed_tasks_by_chat(target_chat)

    # 3. Merge known files into a consolidated index
    known_files = []
    seen_keys = set()

    for item in remote_items:
        fname = item.get("file_name") or ""
        fsize = int(item.get("file_size") or 0)
        norm_name = fname.strip().lower()
        key = (norm_name, fsize) if norm_name and fsize > 0 else (norm_name or f"id_{item['message_id']}", fsize)
        if key not in seen_keys:
            seen_keys.add(key)
            known_files.append({
                "source": "telegram_group",
                "message_id": item["message_id"],
                "file_name": fname,
                "file_size": fsize,
                "media_type": item.get("media_type", "unknown"),
                "date": item.get("date"),
                "sender_id": item.get("sender_id")
            })

    for dbi in db_items:
        fname = dbi.get("file_name") or ""
        fsize = int(dbi.get("file_size") or 0)
        norm_name = fname.strip().lower()
        key = (norm_name, fsize)
        if key not in seen_keys:
            seen_keys.add(key)
            known_files.append({
                "source": "local_database",
                "message_id": dbi.get("message_id"),
                "file_name": fname,
                "file_size": fsize,
                "media_type": "document",
                "date": dbi.get("completed_at")
            })

    # 4. Check for internal duplicates already inside the group
    internal_duplicates = []
    name_size_groups = {}
    for item in known_files:
        fname = (item.get("file_name") or "").strip().lower()
        fsize = int(item.get("file_size") or 0)
        if not fname:
            continue
        grp_key = (fname, fsize)
        if grp_key not in name_size_groups:
            name_size_groups[grp_key] = []
        name_size_groups[grp_key].append(item)

    for (fname, fsize), grp in name_size_groups.items():
        if len(grp) > 1:
            internal_duplicates.append({
                "file_name": grp[0]["file_name"],
                "file_size": fsize,
                "occurrences": len(grp),
                "messages": grp
            })

    # 5. Check incoming files against known files
    file_matches = []
    duplicates_to_skip = []
    unique_files = []

    if req.files:
        for f in req.files:
            fname = f.get("name", "").strip()
            fsize = int(f.get("size", 0))
            norm_name = fname.lower()

            # Exact match: same name and same size
            exact_match = next(
                (k for k in known_files if k["file_name"].strip().lower() == norm_name and k["file_size"] == fsize),
                None
            )
            # Name only match
            name_match = next(
                (k for k in known_files if k["file_name"].strip().lower() == norm_name and k["file_size"] != fsize),
                None
            )
            # Size only match (for files > 100KB)
            size_match = next(
                (k for k in known_files if fsize > 1024 * 100 and k["file_size"] == fsize and k["file_name"].strip().lower() != norm_name),
                None
            )

            if exact_match:
                duplicates_to_skip.append(f)
                file_matches.append({
                    "file": f,
                    "status": "exact_duplicate",
                    "matched_with": exact_match,
                    "reason": f"Already in group (Message #{exact_match.get('message_id', 'DB')})"
                })
            elif name_match:
                file_matches.append({
                    "file": f,
                    "status": "same_name_diff_size",
                    "matched_with": name_match,
                    "reason": f"Same filename exists in Message #{name_match.get('message_id', 'DB')}, but size differs"
                })
                unique_files.append(f)
            elif size_match:
                file_matches.append({
                    "file": f,
                    "status": "same_size_diff_name",
                    "matched_with": size_match,
                    "reason": f"Same file size as '{size_match['file_name']}' in Message #{size_match.get('message_id', 'DB')}"
                })
                unique_files.append(f)
            else:
                unique_files.append(f)
                file_matches.append({
                    "file": f,
                    "status": "unique",
                    "matched_with": None,
                    "reason": "New file, not found in group"
                })

    return {
        "target_chat": target_chat,
        "total_known_files_in_group": len(known_files),
        "remote_scanned_count": len(remote_items),
        "db_history_count": len(db_items),
        "error_notice": error_notice,
        "internal_duplicates": internal_duplicates,
        "checked_files_count": len(req.files) if req.files else 0,
        "duplicate_count": len(duplicates_to_skip),
        "unique_count": len(unique_files),
        "file_matches": file_matches
    }

@app.post("/api/group/delete-messages")
async def delete_group_messages(req: DeleteGroupMessageRequest):
    client = await telegram_manager.get_client()
    if not client or not await client.is_user_authorized():
        raise HTTPException(status_code=400, detail="Telegram client not connected")
    entity = await telegram_manager.resolve_target(req.target_chat)
    await client.delete_messages(entity, req.message_ids)
    return {"success": True, "deleted_ids": req.message_ids}

@app.get("/api/upgrade-streamable/candidates")
async def get_upgrade_candidates(target_chat: str = "", limit: int = 0):
    import sqlite3
    from .config import DATA_DIR
    conn = sqlite3.connect(DATA_DIR / "db" / "uploader.db")
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("""
        SELECT id, file_name, file_size, message_id, file_path, target_chat, created_at
        FROM tasks 
        WHERE status = 'completed' AND target_chat = ?
        ORDER BY id ASC
    """, (target_chat,))
    rows = [dict(r) for r in c.fetchall()]
    conn.close()

    video_exts = ('.mp4', '.mov', '.mkv', '.avi', '.m4v', '.webm')
    candidates = [
        r for r in rows
        if r["file_name"].lower().endswith(video_exts) and os.path.exists(r["file_path"])
    ]
    if limit > 0:
        candidates = candidates[:limit]

    total_bytes = sum(r["file_size"] for r in candidates)
    return {
        "target_chat": target_chat,
        "total_candidates": len(candidates),
        "total_bytes": total_bytes,
        "candidates": candidates
    }

@app.post("/api/upgrade-streamable/start")
async def start_streamable_upgrade(req: UpgradeStreamableRequest, background_tasks: BackgroundTasks):
    client = await telegram_manager.get_client()
    if not client or not await client.is_user_authorized():
        raise HTTPException(status_code=400, detail="Telegram client not connected")

    import sqlite3
    from .config import DATA_DIR
    conn = sqlite3.connect(DATA_DIR / "db" / "uploader.db")
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    if req.task_id:
        c.execute("SELECT * FROM tasks WHERE id = ? AND target_chat = ?", (req.task_id, req.target_chat))
    else:
        c.execute("""
            SELECT * FROM tasks 
            WHERE status = 'completed' AND target_chat = ?
            ORDER BY id ASC
        """, (req.target_chat,))
    rows = [dict(r) for r in c.fetchall()]
    conn.close()

    video_exts = ('.mp4', '.mov', '.mkv', '.avi', '.m4v', '.webm')
    candidates = [
        r for r in rows
        if r["file_name"].lower().endswith(video_exts) and os.path.exists(r["file_path"])
    ]
    if req.limit and req.limit > 0:
        candidates = candidates[:req.limit]

    if not candidates:
        return {"success": False, "message": "No candidate videos found on disk."}

    from upgrade_to_streamable import upgrade_task

    async def run_upgrades():
        for item in candidates:
            try:
                await upgrade_task(client, item, delete_old=req.delete_old)
            except Exception as e:
                logger.error(f"Background upgrade error for task #{item['id']}: {e}")

    background_tasks.add_task(run_upgrades)
    return {
        "success": True,
        "queued_for_upgrade": len(candidates),
        "message": f"Started streamable upgrade for {len(candidates)} videos in the background."
    }

@app.post("/api/auth/bot")
async def login_bot(req: BotLoginRequest):
    try:
        res = await telegram_manager.login_bot(
            bot_token=req.bot_token,
            api_id=req.api_id,
            api_hash=req.api_hash
        )
        queue_manager.trigger()
        return res
    except Exception as e:
        logger.error(f"Bot login error: {e}", exc_info=True)
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/auth/phone/send-code")
async def send_phone_code(req: PhoneSendCodeRequest):
    try:
        res = await telegram_manager.send_phone_code(
            api_id=req.api_id,
            api_hash=req.api_hash,
            phone=req.phone
        )
        return res
    except Exception as e:
        logger.error(f"Send phone code error: {e}", exc_info=True)
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/auth/phone/verify-code")
async def verify_phone_code(req: PhoneVerifyCodeRequest):
    try:
        res = await telegram_manager.sign_in_with_code(
            code=req.code,
            password=req.password
        )
        queue_manager.trigger()
        return res
    except Exception as e:
        logger.error(f"Verify phone code error: {e}", exc_info=True)
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/auth/logout")
async def logout():
    success = await telegram_manager.logout()
    return {"success": success}

@app.post("/api/scan-folder")
async def scan_folder(req: ScanFolderRequest):
    """Scans a local directory or file path and returns matching files and total size."""
    raw_path = os.path.expanduser(req.folder_path.strip())
    p = Path(raw_path).resolve()
    if not p.exists():
        raise HTTPException(status_code=404, detail=f"Path not found: {raw_path}")

    # Check macOS permission
    if p.is_dir():
        try:
            os.listdir(str(p))
        except PermissionError:
            raise HTTPException(
                status_code=403,
                detail=f"macOS Privacy Permission: Terminal/Python is not allowed to access '{p.name}' in this protected folder. "
                       "Easy fix: Just drag and drop the folder directly into the 'Select Media or Folder' box above (your browser has full file access)!"
            )

    if p.is_file():
        return {
            "is_dir": False,
            "count": 1,
            "total_size": p.stat().st_size,
            "files": [{"name": p.name, "path": str(p), "size": p.stat().st_size}]
        }

    # Scan directory
    files = []
    total_size = 0
    pattern = "**/*" if req.recursive else "*"
    for item in p.glob(pattern):
        try:
            if item.is_file() and not item.name.startswith(".") and item.stat().st_size > 0:
                sz = item.stat().st_size
                files.append({
                    "name": item.name,
                    "path": str(item),
                    "size": sz
                })
                total_size += sz
        except (OSError, PermissionError):
            continue

    files.sort(key=lambda x: x["name"])
    return {
        "is_dir": True,
        "count": len(files),
        "total_size": total_size,
        "files": files[:200]
    }

@app.post("/api/upload")
async def upload_files(
    files: List[UploadFile] = File(...),
    target_chat: str = Form(...),
    chat_title: Optional[str] = Form(None),
    caption: Optional[str] = Form(None),
    media_type: str = Form("auto"),
    force_document: bool = Form(False),
    skip_duplicates: bool = Form(False)
):
    """Handles single or multiple browser file uploads, stores locally, and queues for resumable upload."""
    if not files:
        raise HTTPException(status_code=400, detail="No files provided.")

    cfg = load_config()
    chunk_size_kb = int(cfg.get("chunk_size_kb", 512))
    chunk_size = chunk_size_kb * 1024
    max_retries = int(cfg.get("max_retries", 10))

    created_tasks = []
    skipped_count = 0

    try:
        from . import media_utils
        existing_set = set()
        if skip_duplicates:
            existing_items = await db.get_completed_tasks_by_chat(target_chat)
            existing_set = {(x["file_name"].strip().lower(), int(x["file_size"])) for x in existing_items}

        for f in files:
            safe_filename = Path(f.filename).name
            dest_path = UPLOADS_DIR / f"{helpers.generate_random_long()}_{safe_filename}"
            with open(dest_path, "wb") as buffer:
                shutil.copyfileobj(f.file, buffer)

            # Ensure MP4 faststart for instant streaming
            if not force_document and media_type in ("auto", "video"):
                dest_path = Path(media_utils.ensure_mp4_faststart(str(dest_path)))

            file_size = os.path.getsize(dest_path)

            if skip_duplicates and (safe_filename.lower(), file_size) in existing_set:
                logger.info(f"Skipping duplicate file: {safe_filename} ({file_size} bytes)")
                if dest_path.exists():
                    dest_path.unlink()
                skipped_count += 1
                continue

            total_parts = (file_size + chunk_size - 1) // chunk_size
            file_id = helpers.generate_random_long()

            task_id = await db.create_task(
                file_path=str(dest_path),
                file_name=safe_filename,
                file_size=file_size,
                file_id=file_id,
                target_chat=target_chat,
                chat_title=chat_title,
                caption=caption,
                media_type=media_type,
                force_document=force_document,
                chunk_size=chunk_size,
                total_parts=total_parts,
                max_retries=max_retries
            )
            task = await db.get_task(task_id)
            created_tasks.append(task)
            await queue_manager.broadcast("task_queued", {"task": task})

        queue_manager.trigger()
        return {
            "success": True,
            "queued_count": len(created_tasks),
            "skipped_duplicates_count": skipped_count,
            "task_id": created_tasks[0]["id"] if created_tasks else None,
            "task": created_tasks[0] if created_tasks else None,
            "tasks": created_tasks
        }
    except Exception as e:
        logger.error(f"Upload error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/upload-local")
async def upload_local_path(req: LocalFileUploadRequest):
    """Queues a single file or an entire directory of files from the local disk."""
    raw_path = os.path.expanduser(req.file_path.strip())
    p = Path(raw_path).resolve()
    if not p.exists():
        raise HTTPException(status_code=400, detail=f"Path not found: {raw_path}")

    # Check macOS permission
    if p.is_dir():
        try:
            os.listdir(str(p))
        except PermissionError:
            raise HTTPException(
                status_code=403,
                detail=f"macOS Privacy Permission: Terminal/Python is not allowed to access '{p.name}' in this protected folder. "
                       "Easy fix: Just drag and drop the folder directly into the 'Select Media or Folder' box above (your browser has full file access)!"
            )

    # Collect files to queue
    files_to_queue = []
    if p.is_file():
        files_to_queue.append(p)
    elif p.is_dir():
        pattern = "**/*" if req.recursive else "*"
        for item in p.glob(pattern):
            try:
                if item.is_file() and not item.name.startswith(".") and item.stat().st_size > 0:
                    files_to_queue.append(item)
            except (OSError, PermissionError):
                continue
        if not files_to_queue:
            hint = " (If it contains subfolders, make sure 'Include subfolders recursively' is checked, or drag and drop the folder into the box above)."
            raise HTTPException(status_code=400, detail=f"No valid files found in directory: {raw_path}{hint}")

    cfg = load_config()
    chunk_size_kb = int(cfg.get("chunk_size_kb", 512))
    chunk_size = chunk_size_kb * 1024
    max_retries = int(cfg.get("max_retries", 10))

    created_tasks = []
    skipped_count = 0
    try:
        existing_set = set()
        if req.skip_duplicates:
            existing_items = await db.get_completed_tasks_by_chat(req.target_chat)
            existing_set = {(x["file_name"].strip().lower(), int(x["file_size"])) for x in existing_items}

        for file_item in files_to_queue:
            file_size = file_item.stat().st_size
            if req.skip_duplicates and (file_item.name.lower(), file_size) in existing_set:
                logger.info(f"Skipping duplicate local file: {file_item.name} ({file_size} bytes)")
                skipped_count += 1
                continue

            total_parts = (file_size + chunk_size - 1) // chunk_size
            file_id = helpers.generate_random_long()

            task_id = await db.create_task(
                file_path=str(file_item),
                file_name=file_item.name,
                file_size=file_size,
                file_id=file_id,
                target_chat=req.target_chat,
                chat_title=req.chat_title,
                caption=req.caption,
                media_type=req.media_type,
                force_document=req.force_document,
                chunk_size=chunk_size,
                total_parts=total_parts,
                max_retries=max_retries
            )
            task = await db.get_task(task_id)
            created_tasks.append(task)
            await queue_manager.broadcast("task_queued", {"task": task})

        queue_manager.trigger()
        return {
            "success": True,
            "queued_count": len(created_tasks),
            "skipped_duplicates_count": skipped_count,
            "task_id": created_tasks[0]["id"] if created_tasks else None,
            "task": created_tasks[0] if created_tasks else None,
            "tasks": created_tasks
        }
    except Exception as e:
        logger.error(f"Local file/dir queue error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/tasks")
async def list_tasks(status: Optional[str] = None, limit: int = 100):
    tasks = await db.list_tasks(status=status, limit=limit)
    return {"tasks": tasks}

@app.get("/api/tasks/{task_id}")
async def get_task(task_id: int):
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    completed_parts = await db.get_completed_parts(task_id)
    return {
        "task": task,
        "completed_parts_count": len(completed_parts),
        "completed_parts": sorted(list(completed_parts))
    }

@app.post("/api/tasks/{task_id}/pause")
async def pause_task(task_id: int):
    await queue_manager.pause_task(task_id)
    return {"success": True}

@app.post("/api/tasks/{task_id}/resume")
async def resume_task(task_id: int):
    await queue_manager.resume_task(task_id)
    return {"success": True}

@app.post("/api/tasks/{task_id}/retry")
async def retry_task(task_id: int):
    await queue_manager.retry_task_now(task_id)
    return {"success": True}

@app.delete("/api/tasks/{task_id}")
async def delete_task(task_id: int):
    await queue_manager.cancel_task(task_id)
    success = await db.delete_task(task_id)
    return {"success": success}

@app.post("/api/tasks/clear-completed")
async def clear_completed():
    count = await db.clear_completed_tasks()
    return {"cleared_count": count}

@app.get("/api/cache/stats")
async def get_cache_stats():
    """Returns sizes and file counts of reclaimable upload staging and thumbnail caches."""
    protected_paths = await db.get_protected_upload_paths()
    uploads_scan = _scan_cache_directory(UPLOADS_DIR, protected_paths)
    thumbs_scan = _scan_cache_directory(THUMBS_DIR, set())

    uploads_bytes = uploads_scan["reclaimable_bytes"]
    thumbs_bytes = thumbs_scan["reclaimable_bytes"]
    return {
        "uploads_dir": str(UPLOADS_DIR),
        "thumbs_dir": str(THUMBS_DIR),
        "uploads_bytes": uploads_bytes,
        "uploads_count": len(uploads_scan["reclaimable"]),
        "thumbs_bytes": thumbs_bytes,
        "thumbs_count": len(thumbs_scan["reclaimable"]),
        "protected_bytes": uploads_scan["protected_bytes"],
        "protected_count": len(uploads_scan["protected"]),
        "reclaimable_bytes": uploads_bytes + thumbs_bytes,
        "reclaimable_count": len(uploads_scan["reclaimable"]) + len(thumbs_scan["reclaimable"]),
    }

@app.post("/api/cache/clear")
async def clear_upload_cache(req: Optional[ClearCacheRequest] = None):
    """Deletes staged upload and thumbnail cache files while strictly preserving active queue files."""
    include_thumbs = True if req is None else req.include_thumbs
    protected_paths = await db.get_protected_upload_paths()
    uploads_scan = _scan_cache_directory(UPLOADS_DIR, protected_paths)

    deleted_files = 0
    freed_bytes = 0

    for item in uploads_scan["reclaimable"]:
        try:
            size = item.stat().st_size
            item.unlink()
            deleted_files += 1
            freed_bytes += size
        except OSError as e:
            logger.warning(f"Failed to remove cached upload {item}: {e}")

    # Clean up empty subdirectories inside UPLOADS_DIR
    for sub in sorted(UPLOADS_DIR.rglob("*"), reverse=True):
        if sub.is_dir():
            try:
                sub.rmdir()
            except OSError:
                pass

    if include_thumbs:
        has_active_tasks = len(protected_paths) > 0
        import time as _time
        now_ts = _time.time()
        thumbs_scan = _scan_cache_directory(THUMBS_DIR, set())
        for item in thumbs_scan["reclaimable"]:
            try:
                # If uploads are actively running, keep very recent thumbnails (<120s) to avoid send_file races
                if has_active_tasks and (now_ts - item.stat().st_mtime) < 120:
                    continue
                size = item.stat().st_size
                item.unlink()
                deleted_files += 1
                freed_bytes += size
            except OSError as e:
                logger.warning(f"Failed to remove cached thumbnail {item}: {e}")

    updated_stats = await get_cache_stats()
    logger.info(f"Cleared upload cache: deleted {deleted_files} files ({freed_bytes} bytes freed)")
    return {
        "success": True,
        "deleted_files_count": deleted_files,
        "freed_bytes": freed_bytes,
        "skipped_protected_count": len(uploads_scan["protected"]),
        "stats": updated_stats,
    }

@app.get("/api/settings")
async def get_settings():
    cfg = load_config()
    return {"settings": cfg}

@app.post("/api/settings")
async def update_settings(req: SettingsUpdateRequest):
    updates = {k: v for k, v in req.model_dump().items() if v is not None}
    saved = save_config(updates)
    return {"success": True, "settings": saved}

@app.websocket("/ws/progress")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    subscriber_queue = queue_manager.subscribe()
    try:
        # Initial greeting with stats
        stats = await db.get_dashboard_stats()
        await websocket.send_json({
            "type": "init",
            "stats": stats
        })

        while True:
            # Receive message from queue manager or shutdown sentinel
            event = await subscriber_queue.get()
            if event is None:
                break
            await websocket.send_json(event)
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    finally:
        queue_manager.unsubscribe(subscriber_queue)
