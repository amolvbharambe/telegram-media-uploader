import aiosqlite
import datetime
import time
from pathlib import Path
from contextlib import asynccontextmanager
from typing import List, Optional, Dict, Any, Set
from .config import DB_PATH

_db_initialized = False

@asynccontextmanager
async def get_connection():
    global _db_initialized
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA foreign_keys = ON;")
        await conn.execute("PRAGMA journal_mode = WAL;")
        if not _db_initialized:
            cursor = await conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='tasks'")
            if not await cursor.fetchone():
                await _create_tables(conn)
            _db_initialized = True
        yield conn

async def _create_tables(conn: aiosqlite.Connection):
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_path TEXT NOT NULL,
            file_name TEXT NOT NULL,
            file_size INTEGER NOT NULL,
            file_id INTEGER NOT NULL,
            target_chat TEXT NOT NULL,
            chat_title TEXT,
            caption TEXT,
            media_type TEXT DEFAULT 'auto',
            force_document INTEGER DEFAULT 0,
            chunk_size INTEGER NOT NULL,
            total_parts INTEGER NOT NULL,
            completed_parts INTEGER DEFAULT 0,
            bytes_uploaded INTEGER DEFAULT 0,
            status TEXT DEFAULT 'queued',
            retry_count INTEGER DEFAULT 0,
            max_retries INTEGER DEFAULT 10,
            next_retry_at REAL DEFAULT 0,
            error_message TEXT,
            message_id INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT
        );
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS task_parts (
            task_id INTEGER NOT NULL,
            part_index INTEGER NOT NULL,
            part_size INTEGER NOT NULL,
            uploaded_at REAL NOT NULL,
            PRIMARY KEY (task_id, part_index),
            FOREIGN KEY (task_id) REFERENCES tasks (id) ON DELETE CASCADE
        );
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS known_dialogs (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            username TEXT,
            type TEXT DEFAULT 'group',
            updated_at REAL NOT NULL
        );
    """)
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_task_parts_task ON task_parts(task_id);")
    await conn.commit()

async def init_db():
    """Initializes the database tables and indexes."""
    async with get_connection() as conn:
        await _create_tables(conn)

async def create_task(
    file_path: str,
    file_name: str,
    file_size: int,
    file_id: int,
    target_chat: str,
    chat_title: Optional[str] = None,
    caption: Optional[str] = None,
    media_type: str = "auto",
    force_document: bool = False,
    chunk_size: int = 524288,
    total_parts: int = 1,
    max_retries: int = 10
) -> int:
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            INSERT INTO tasks (
                file_path, file_name, file_size, file_id,
                target_chat, chat_title, caption, media_type,
                force_document, chunk_size, total_parts,
                completed_parts, bytes_uploaded, status,
                retry_count, max_retries, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 'queued', 0, ?, ?, ?)
            """,
            (
                file_path, file_name, file_size, file_id,
                target_chat, chat_title, caption, media_type,
                1 if force_document else 0, chunk_size, total_parts,
                max_retries, now, now
            )
        )
        task_id = cursor.lastrowid
        await conn.commit()
        return task_id

async def get_task(task_id: int) -> Optional[Dict[str, Any]]:
    async with get_connection() as conn:
        cursor = await conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
        row = await cursor.fetchone()
        if row:
            return dict(row)
        return None

async def list_tasks(
    status: Optional[str] = None,
    limit: int = 100,
    offset: int = 0
) -> List[Dict[str, Any]]:
    async with get_connection() as conn:
        if status:
            cursor = await conn.execute(
                "SELECT * FROM tasks WHERE status = ? ORDER BY id DESC LIMIT ? OFFSET ?",
                (status, limit, offset)
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

        # When listing all tasks at offset 0, always include all active/non-completed tasks
        # plus recent completed/cancelled tasks up to the limit so active work never hides.
        if offset == 0:
            cursor = await conn.execute(
                """
                SELECT * FROM (
                    SELECT * FROM tasks WHERE status NOT IN ('completed', 'cancelled')
                    UNION
                    SELECT * FROM (
                        SELECT * FROM tasks ORDER BY id DESC LIMIT ?
                    )
                )
                ORDER BY id DESC
                """,
                (limit,)
            )
        else:
            cursor = await conn.execute(
                "SELECT * FROM tasks ORDER BY id DESC LIMIT ? OFFSET ?",
                (limit, offset)
            )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

async def get_completed_tasks_by_chat(target_chat: str) -> List[Dict[str, Any]]:
    async with get_connection() as conn:
        cursor = await conn.execute(
            """SELECT id, file_name, file_size, target_chat, chat_title, message_id, completed_at
               FROM tasks
               WHERE status = 'completed' AND (target_chat = ? OR target_chat LIKE ?)
               ORDER BY id DESC""",
            (str(target_chat), f"%{target_chat}%")
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

async def update_task_status(
    task_id: int,
    status: str,
    error_message: Optional[str] = None,
    message_id: Optional[int] = None,
    next_retry_at: Optional[float] = None,
    clear_error: bool = False,
    reset_retry: bool = False
):
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    completed_at = now if status == "completed" else None
    async with get_connection() as conn:
        query = """
            UPDATE tasks 
            SET status = ?, 
                error_message = CASE WHEN ? THEN NULL ELSE COALESCE(?, error_message) END,
                message_id = COALESCE(?, message_id),
                next_retry_at = COALESCE(?, next_retry_at),
                retry_count = CASE WHEN ? THEN 0 ELSE retry_count END,
                completed_at = CASE WHEN ? = 'completed' THEN ? ELSE completed_at END,
                updated_at = ?
            WHERE id = ?
        """
        await conn.execute(
            query,
            (
                status,
                1 if clear_error else 0,
                error_message,
                message_id,
                next_retry_at,
                1 if reset_retry else 0,
                status,
                completed_at,
                now,
                task_id,
            )
        )
        await conn.commit()

async def record_part_upload(task_id: int, part_index: int, part_size: int, timestamp: float):
    """Records that a part has been uploaded and updates tasks totals."""
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    async with get_connection() as conn:
        await conn.execute(
            """
            INSERT OR IGNORE INTO task_parts (task_id, part_index, part_size, uploaded_at)
            VALUES (?, ?, ?, ?)
            """,
            (task_id, part_index, part_size, timestamp)
        )
        cursor = await conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(part_size), 0) FROM task_parts WHERE task_id = ?",
            (task_id,)
        )
        completed_count, bytes_uploaded = await cursor.fetchone()
        
        await conn.execute(
            """
            UPDATE tasks 
            SET completed_parts = ?, bytes_uploaded = ?, updated_at = ?
            WHERE id = ?
            """,
            (completed_count, bytes_uploaded, now, task_id)
        )
        await conn.commit()

async def get_completed_parts(task_id: int) -> Set[int]:
    """Returns set of part_indexes already uploaded for this task."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT part_index FROM task_parts WHERE task_id = ?",
            (task_id,)
        )
        rows = await cursor.fetchall()
        return {row[0] for row in rows}

async def delete_task_part(task_id: int, part_index: int):
    """Removes a single missing/corrupt part record so it can be re-uploaded."""
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    async with get_connection() as conn:
        await conn.execute(
            "DELETE FROM task_parts WHERE task_id = ? AND part_index = ?",
            (task_id, part_index)
        )
        cursor = await conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(part_size), 0) FROM task_parts WHERE task_id = ?",
            (task_id,)
        )
        completed_count, bytes_uploaded = await cursor.fetchone()
        await conn.execute(
            """
            UPDATE tasks
            SET completed_parts = ?, bytes_uploaded = ?, updated_at = ?
            WHERE id = ?
            """,
            (completed_count, bytes_uploaded, now, task_id)
        )
        await conn.commit()

async def reset_task_parts(task_id: int, new_file_id: int):
    """Resets parts if Telegram discarded remote parts and new file_id is required."""
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    async with get_connection() as conn:
        await conn.execute("DELETE FROM task_parts WHERE task_id = ?", (task_id,))
        await conn.execute(
            """
            UPDATE tasks 
            SET file_id = ?, completed_parts = 0, bytes_uploaded = 0, updated_at = ?
            WHERE id = ?
            """,
            (new_file_id, now, task_id)
        )
        await conn.commit()

async def increment_task_retry(task_id: int, error_message: str, next_retry_at: float):
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    async with get_connection() as conn:
        await conn.execute(
            """
            UPDATE tasks 
            SET retry_count = retry_count + 1,
                error_message = ?,
                next_retry_at = ?,
                status = 'retrying',
                updated_at = ?
            WHERE id = ?
            """,
            (error_message, next_retry_at, now, task_id)
        )
        await conn.commit()

async def delete_task(task_id: int) -> bool:
    async with get_connection() as conn:
        cursor = await conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        await conn.commit()
        return cursor.rowcount > 0

async def clear_completed_tasks() -> int:
    async with get_connection() as conn:
        cursor = await conn.execute("DELETE FROM tasks WHERE status IN ('completed', 'failed', 'cancelled')")
        await conn.commit()
        return cursor.rowcount

async def get_protected_upload_paths() -> Set[str]:
    """Returns resolved file paths referenced by active, paused, queued, or retrying tasks."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            SELECT file_path FROM tasks
            WHERE status IN ('queued', 'uploading', 'retrying', 'paused')
              AND file_path IS NOT NULL
            """
        )
        rows = await cursor.fetchall()
        protected: Set[str] = set()
        for row in rows:
            raw_path = row["file_path"]
            if raw_path:
                try:
                    protected.add(str(Path(raw_path).resolve()))
                except Exception:
                    protected.add(str(raw_path))
        return protected

async def get_pending_or_resumable_tasks() -> List[Dict[str, Any]]:
    """Returns tasks that should be processed (queued, uploading that was interrupted, or retrying)."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            SELECT * FROM tasks 
            WHERE status IN ('queued', 'uploading', 'retrying')
            ORDER BY id ASC
            """
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

async def get_dashboard_stats() -> Dict[str, Any]:
    async with get_connection() as conn:
        cursor = await conn.execute("""
            SELECT 
                COUNT(*) as total_tasks,
                COALESCE(SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END), 0) as completed_tasks,
                COALESCE(SUM(CASE WHEN status IN ('uploading', 'queued', 'retrying') THEN 1 ELSE 0 END), 0) as active_tasks,
                COALESCE(SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END), 0) as failed_tasks,
                COALESCE(SUM(CASE WHEN status = 'paused' THEN 1 ELSE 0 END), 0) as paused_tasks,
                COALESCE(SUM(CASE WHEN status = 'completed' THEN file_size ELSE bytes_uploaded END), 0) as total_bytes_transferred
            FROM tasks
        """)
        row = await cursor.fetchone()
        return dict(row)

async def save_known_dialog(dialog_id: str, title: str, username: Optional[str] = None, d_type: str = "group") -> None:
    """Saves or updates a Telegram dialog/group in persistent SQLite storage."""
    async with get_connection() as conn:
        await conn.execute("""
            INSERT INTO known_dialogs (id, title, username, type, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                title = excluded.title,
                username = COALESCE(excluded.username, known_dialogs.username),
                type = excluded.type,
                updated_at = excluded.updated_at
        """, (str(dialog_id), title, username, d_type, time.time()))
        await conn.commit()

async def get_known_dialogs() -> List[Dict[str, Any]]:
    """Retrieves all known Telegram dialogs, seeded from known_dialogs and existing tasks history."""
    async with get_connection() as conn:
        cursor = await conn.execute("""
            SELECT id, title, username, type FROM known_dialogs ORDER BY updated_at DESC
        """)
        rows = await cursor.fetchall()
        dialogs = [dict(r) for r in rows]

        # Backfill from tasks table if any known group has not been saved yet
        cursor2 = await conn.execute("""
            SELECT DISTINCT target_chat, chat_title FROM tasks WHERE target_chat IS NOT NULL
        """)
        task_rows = await cursor2.fetchall()
        seen = {str(d["id"]) for d in dialogs}
        for tr in task_rows:
            cid = str(tr["target_chat"])
            if cid not in seen:
                ctitle = tr["chat_title"] or cid
                cleaned_title = (
                    ctitle.replace("👥 ", "")
                          .replace("📢 ", "")
                          .replace(" (group)", "")
                          .replace(" (channel)", "")
                          .strip()
                )
                d_item = {
                    "id": cid,
                    "title": cleaned_title,
                    "username": None,
                    "type": "group"
                }
                dialogs.append(d_item)
                seen.add(cid)
                try:
                    await conn.execute("""
                        INSERT OR IGNORE INTO known_dialogs (id, title, username, type, updated_at)
                        VALUES (?, ?, ?, ?, ?)
                    """, (cid, cleaned_title, None, "group", time.time()))
                except Exception:
                    pass
        await conn.commit()
        return dialogs
