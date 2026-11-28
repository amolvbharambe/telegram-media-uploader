#!/usr/bin/env python3
"""
Telegram Resumable Media Uploader - Upgrade Old Videos to Streamable
Re-uploads previously uploaded non-streamable videos with instant-streaming attributes (faststart + supports_streaming=True)
and automatically deletes the old non-streamable message from the Telegram group.
"""
import os
import sys
import asyncio
import logging
import sqlite3
from pathlib import Path
from typing import Optional, List

from app.config import DATA_DIR, load_config
from app import db
from app.telegram_client import telegram_manager
from app.resumable_uploader import ResumableUploader
from app import media_utils
from telethon import helpers

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("upgrade_streamable")

async def upgrade_task(client, task_row: dict, delete_old: bool = True) -> Optional[int]:
    task_id = task_row["id"]
    file_path = task_row["file_path"]
    file_name = task_row["file_name"]
    old_msg_id = task_row["message_id"]
    target_chat = task_row["target_chat"]

    if not os.path.exists(file_path):
        logger.warning(f"Task #{task_id}: File not found on disk: {file_path}")
        return None

    logger.info(f"--- Upgrading Task #{task_id}: {file_name} (Old Telegram Msg #{old_msg_id}) ---")

    # 1. Ensure faststart on the local video file (lossless)
    file_path = media_utils.ensure_mp4_faststart(file_path)
    file_size = os.path.getsize(file_path)

    # 2. Prepare chunking
    cfg = load_config()
    chunk_size = int(cfg.get("chunk_size_kb", 512)) * 1024
    total_parts = (file_size + chunk_size - 1) // chunk_size
    new_file_id = helpers.generate_random_long()

    # 3. Create fresh upgrade task record in DB
    new_task_id = await db.create_task(
        file_path=file_path,
        file_name=file_name,
        file_size=file_size,
        file_id=new_file_id,
        target_chat=target_chat,
        caption=task_row.get("caption") or "",
        media_type="video",
        force_document=False,
        chunk_size=chunk_size,
        total_parts=total_parts
    )

    # 4. Upload with streaming enabled
    def on_progress(p):
        pct = p["percent"]
        speed = f"{p['speed'] / (1024*1024):.1f} MB/s" if p.get("speed") else ""
        sys.stdout.write(f"\r  Uploading {file_name}: {pct:5.1f}% | {speed} ")
        sys.stdout.flush()

    uploader = ResumableUploader(
        client=client,
        task_id=new_task_id,
        progress_callback=on_progress,
        max_concurrent=4
    )

    try:
        new_msg_id = await uploader.upload()
        sys.stdout.write("\n")
        logger.info(f"  Successfully uploaded as streamable video! New Message ID: #{new_msg_id}")

        # 5. Delete old non-streamable message from group
        if delete_old and old_msg_id:
            try:
                target_entity = await telegram_manager.resolve_target(target_chat)
                await client.delete_messages(target_entity, [old_msg_id])
                logger.info(f"  Cleaned up old non-streamable Message #{old_msg_id} from group.")
            except Exception as e:
                logger.warning(f"  Failed to delete old message #{old_msg_id}: {e}")

        return new_msg_id
    except Exception as e:
        sys.stdout.write("\n")
        logger.error(f"  Failed to upgrade task #{task_id}: {e}")
        return None

async def main():
    import argparse
    parser = argparse.ArgumentParser(description="Upgrade old uploaded videos to streamable format")
    parser.add_argument("--chat", default="", help="Target Telegram chat ID (e.g. -100..., @channel, or group ID)")
    parser.add_argument("--task-id", type=int, help="Upgrade a single specific task ID")
    parser.add_argument("--limit", type=int, default=0, help="Limit number of videos to upgrade (0 for all)")
    parser.add_argument("--keep-old", action="store_true", help="Do not delete the old message after uploading")
    parser.add_argument("--dry-run", action="store_true", help="List videos that would be upgraded without executing")
    args = parser.parse_args()

    await db.init_db()

    conn = sqlite3.connect(DATA_DIR / "db" / "uploader.db")
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    if args.task_id:
        c.execute("SELECT * FROM tasks WHERE id = ?", (args.task_id,))
    else:
        c.execute("""
            SELECT * FROM tasks 
            WHERE status = 'completed' AND target_chat = ?
            ORDER BY id ASC
        """, (args.chat,))

    rows = [dict(r) for r in c.fetchall()]
    conn.close()

    video_exts = ('.mp4', '.mov', '.mkv', '.avi', '.m4v')
    candidate_videos = [r for r in rows if r["file_name"].lower().endswith(video_exts) and os.path.exists(r["file_path"])]

    if args.limit > 0:
        candidate_videos = candidate_videos[:args.limit]

    print(f"\nFound {len(candidate_videos)} candidate video(s) for streamable upgrade in chat {args.chat}.")
    if not candidate_videos:
        print("No videos found to upgrade.")
        return

    total_size = sum(r["file_size"] for r in candidate_videos) / (1024 * 1024 * 1024)
    print(f"Total size: {total_size:.2f} GB across {len(candidate_videos)} files.")

    if args.dry_run:
        print("\n--- Dry Run List ---")
        for v in candidate_videos[:15]:
            print(f"  Task #{v['id']}: {v['file_name']} ({v['file_size'] / (1024*1024):.1f} MB) -> Old Msg #{v['message_id']}")
        if len(candidate_videos) > 15:
            print(f"  ... and {len(candidate_videos) - 15} more.")
        return

    client = await telegram_manager.initialize()
    if not client or not await client.is_user_authorized():
        print("Error: Telegram client is not authenticated. Please log in first.")
        return

    print(f"\nStarting upgrade of {len(candidate_videos)} video(s)...")
    success_count = 0
    for idx, v in enumerate(candidate_videos, 1):
        print(f"\n[{idx}/{len(candidate_videos)}] Processing {v['file_name']}...")
        new_id = await upgrade_task(client, v, delete_old=not args.keep_old)
        if new_id:
            success_count += 1

    print(f"\nUpgrade complete! {success_count}/{len(candidate_videos)} videos upgraded successfully to streamable format.")

if __name__ == "__main__":
    asyncio.run(main())
