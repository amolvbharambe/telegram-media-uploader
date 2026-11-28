#!/usr/bin/env python3
"""
Telegram Resumable Media Uploader CLI
Upload media to Telegram chats/groups with automatic resume support.
"""
import os
import sys
import time
import asyncio
import argparse
from pathlib import Path

from app.config import load_config, save_config, BASE_DIR
from app import db
from app.telegram_client import telegram_manager
from app.resumable_uploader import ResumableUploader
from telethon import helpers

def format_bytes(num_bytes: int) -> str:
    for unit in ['B', 'KB', 'MB', 'GB']:
        if abs(num_bytes) < 1024.0:
            return f"{num_bytes:3.1f} {unit}"
        num_bytes /= 1024.0
    return f"{num_bytes:.1f} TB"

async def cmd_auth(args):
    """Handles Telegram authentication."""
    cfg = load_config()
    api_id = args.api_id or cfg.get("api_id") or 2040
    api_hash = args.api_hash or cfg.get("api_hash") or "b18441a1ff607e10a989891a5462e627"

    if args.bot_token:
        print(f"Logging in as Bot with token...")
        res = await telegram_manager.login_bot(bot_token=args.bot_token, api_id=int(api_id), api_hash=api_hash)
        print("Successfully authenticated as Bot:", res["user"])
    elif args.phone:
        print(f"Sending code to {args.phone}...")
        res = await telegram_manager.send_phone_code(int(api_id), api_hash, args.phone)
        code = input("Enter verification code received: ").strip()
        pwd = input("Enter 2FA password (leave empty if none): ").strip() or None
        auth_res = await telegram_manager.sign_in_with_code(code, pwd)
        print("Successfully authenticated as User:", auth_res["user"])
    else:
        print("Specify either --bot-token or --phone")

async def cmd_upload(args):
    """Uploads a file or directory to Telegram with chunked resumable upload."""
    await db.init_db()
    raw_path = os.path.expanduser(args.file)
    p = Path(raw_path).resolve()
    if not p.exists():
        print(f"Error: Path not found: {raw_path}")
        return

    files_to_upload = []
    if p.is_file():
        files_to_upload.append(p)
    elif p.is_dir():
        pattern = "**/*" if args.recursive else "*"
        for item in p.glob(pattern):
            if item.is_file() and not item.name.startswith(".") and item.stat().st_size > 0:
                files_to_upload.append(item)
        if not files_to_upload:
            print(f"Error: No valid files found in directory: {p}")
            return
        print(f"Found {len(files_to_upload)} files in {p}")

    client = await telegram_manager.initialize()
    if not client or not await client.is_user_authorized():
        print("Telegram client is not authorized. Run `python cli.py auth` first.")
        return

    cfg = load_config()
    chunk_size_kb = int(cfg.get("chunk_size_kb", 512))
    chunk_size = chunk_size_kb * 1024

    for idx, file_path in enumerate(files_to_upload, 1):
        file_size = file_path.stat().st_size
        total_parts = (file_size + chunk_size - 1) // chunk_size
        file_id = helpers.generate_random_long()

        task_id = await db.create_task(
            file_path=str(file_path),
            file_name=file_path.name,
            file_size=file_size,
            file_id=file_id,
            target_chat=args.chat,
            caption=args.caption,
            media_type=args.media_type,
            force_document=args.document,
            chunk_size=chunk_size,
            total_parts=total_parts
        )
        print(f"\n[{idx}/{len(files_to_upload)}] Created task #{task_id} for {file_path.name} ({format_bytes(file_size)})")

        def print_progress(info):
            pct = info["percent"]
            uploaded = format_bytes(info["uploaded_bytes"])
            total = format_bytes(info["total_bytes"])
            speed = format_bytes(info["speed"]) + "/s"
            eta = f"{round(info['eta'])}s" if info['eta'] > 0 else "0s"
            sys.stdout.write(f"\rProgress: [{pct:5.1f}%] {uploaded} / {total} | {speed} | ETA: {eta} | Part {info['completed_parts']}/{info['total_parts']}")
            sys.stdout.flush()

        uploader = ResumableUploader(
            client=client,
            task_id=task_id,
            progress_callback=print_progress,
            max_concurrent=args.concurrency
        )

        try:
            msg_id = await uploader.upload()
            print(f"\nDone! Delivered {file_path.name} to {args.chat} (Message ID: {msg_id})")
        except Exception as e:
            print(f"\nUpload interrupted on {file_path.name}: {e}")
            print("Task saved in database. Run `python cli.py resume` to resume!")

async def cmd_queue(args):
    """Displays queued and resumable tasks."""
    await db.init_db()
    tasks = await db.list_tasks(limit=50)
    if not tasks:
        print("No tasks found in queue.")
        return

    print(f"{'ID':<5} {'Status':<12} {'File Name':<30} {'Uploaded':<15} {'Total':<15} {'Target'}")
    print("-" * 90)
    for t in tasks:
        uploaded = format_bytes(t["bytes_uploaded"])
        total = format_bytes(t["file_size"])
        print(f"{t['id']:<5} {t['status']:<12} {t['file_name'][:28]:<30} {uploaded:<15} {total:<15} {t['target_chat']}")

async def cmd_resume(args):
    """Resumes all pending or interrupted tasks."""
    await db.init_db()
    tasks = await db.get_pending_or_resumable_tasks()
    if not tasks:
        print("No resumable or pending tasks.")
        return

    client = await telegram_manager.initialize()
    if not client or not await client.is_user_authorized():
        print("Telegram client is not authorized. Run `python cli.py auth` first.")
        return

    for t in tasks:
        print(f"\nResuming task #{t['id']}: {t['file_name']} (already uploaded: {format_bytes(t['bytes_uploaded'])})")
        def print_progress(info):
            pct = info["percent"]
            uploaded = format_bytes(info["uploaded_bytes"])
            total = format_bytes(info["total_bytes"])
            speed = format_bytes(info["speed"]) + "/s"
            sys.stdout.write(f"\rProgress: [{pct:5.1f}%] {uploaded} / {total} | {speed} | Part {info['completed_parts']}/{info['total_parts']}")
            sys.stdout.flush()

        uploader = ResumableUploader(client=client, task_id=t["id"], progress_callback=print_progress)
        try:
            msg_id = await uploader.upload()
            print(f"\nCompleted task #{t['id']}! (Message ID: {msg_id})")
        except Exception as e:
            print(f"\nTask #{t['id']} paused or error: {e}")

def main():
    parser = argparse.ArgumentParser(description="Telegram Resumable Media Uploader")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Auth
    auth_parser = subparsers.add_parser("auth", help="Authenticate with Telegram")
    auth_parser.add_argument("--api-id", type=int, help="Telegram API ID")
    auth_parser.add_argument("--api-hash", type=str, help="Telegram API Hash")
    auth_parser.add_argument("--bot-token", type=str, help="Bot Token from @BotFather")
    auth_parser.add_argument("--phone", type=str, help="Phone number in international format (+1...)")

    # Upload
    upload_parser = subparsers.add_parser("upload", help="Upload media file or folder with auto-resume")
    upload_parser.add_argument("file", help="Path to media file or folder")
    upload_parser.add_argument("--chat", required=True, help="Target chat ID, username or @channel")
    upload_parser.add_argument("--caption", default="", help="Optional caption")
    upload_parser.add_argument("--media-type", default="auto", choices=["auto", "video", "photo", "audio", "document"])
    upload_parser.add_argument("--document", action="store_true", help="Send as uncompressed document")
    upload_parser.add_argument("--recursive", "-r", action="store_true", help="If uploading a directory, include subfolders recursively")
    upload_parser.add_argument("--concurrency", type=int, default=3, help="Max concurrent chunks (default: 3)")

    # Queue
    subparsers.add_parser("queue", help="List all upload tasks in queue")

    # Resume
    subparsers.add_parser("resume", help="Resume pending/interrupted uploads")

    args = parser.parse_args()

    if args.command == "auth":
        asyncio.run(cmd_auth(args))
    elif args.command == "upload":
        asyncio.run(cmd_upload(args))
    elif args.command == "queue":
        asyncio.run(cmd_queue(args))
    elif args.command == "resume":
        asyncio.run(cmd_resume(args))

if __name__ == "__main__":
    main()
