# 🚀 Telegram Resumable Media Uploader

A robust, high-performance application built to upload media (photos, streamable videos, audio files, documents, and large archives up to 2GB+) to Telegram chats, groups, and channels with **stateful chunk recovery** and **automatic auto-resume** upon failure or network interruption.

[![Official User Guide](https://img.shields.io/badge/PDF_Guide-Download_User_Guide-red?logo=adobeacrobatreader)](docs/Telegram_Uploader_Complete_User_Guide.pdf)
[![macOS App](https://img.shields.io/badge/macOS_App-Download_ZIP-000000?logo=apple)](https://github.com/amolvbharambe/telegram-media-uploader/releases/latest)
[![Android App](https://img.shields.io/badge/Android_App-Download_APK-green?logo=android)](https://github.com/amolvbharambe/telegram-media-uploader/releases/latest)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

---

## 📖 Complete Setup & User Guide (PDF)

A clean, step-by-step setup guide is available directly in this repository for setting up your own Telegram bot and groups without exposing any personal data:

📄 **[Download Complete Setup & User Guide (PDF)](docs/Telegram_Uploader_Complete_User_Guide.pdf)**

Inside the guide:
- Setting up your own Bot with `@BotFather`
- Obtaining Telegram API ID & Hash from `my.telegram.org`
- Adding your Bot to Groups & Channels correctly
- Running the Web Dashboard on your Mac/Linux/Windows PC
- Installing and using the native Android App (`TelegramUploader.apk`)
- How instant video streaming and lossless faststart remuxing work
- Group duplicate prevention and troubleshooting

---

## ✨ Key Features

- **Chunked Resumable Upload Engine (MTProto)**:
  - Files are split into MTProto chunks (typically 256 KB or 512 KB).
  - Every successfully uploaded chunk is persisted to an SQLite database with its byte offset and timestamp.
  - If a connection drops, power fails, or an upload is interrupted, **already uploaded chunks are preserved** and the upload automatically resumes from the exact failed part without restarting from zero.
- **Instant Progressive Video Streaming (Lossless Faststart)**:
  - Video files (MP4, MOV, MKV, etc.) automatically undergo non-destructive index remuxing (`ffmpeg -c copy -movflags +faststart`) to position the `moov` atom at byte 0.
  - Uploaded with `DocumentAttributeVideo(supports_streaming=True)` so recipients can stream videos instantly in Telegram without downloading the full file first.
  - **Zero re-encoding**: 100% exact original bit-for-bit quality, resolution, bitrate, and colors preserved.
- **In-Group Duplicate Detection & Prevention**:
  - Automatically scans Telegram group messages and local indexed task records.
  - Compares filename, byte size, and signatures to highlight duplicates before upload.
  - Includes a one-click **⚡ Skip Duplicates** button to save bandwidth.
- **Autonomous Auto-Resume**:
  - Automatically handles network drops, transient socket timeouts, and Telegram rate limits (`FloodWaitError`).
  - Exponential backoff with jitter on transient failures.
  - On app launch/restart, any interrupted or pending uploads are detected and **resumed automatically**.
- **Modern Responsive Web UI**:
  - Clean dashboard with real-time statistics (Active uploads, completed count, total data delivered).
  - **Multi-File Drag & Drop**: Drag and drop dozens or hundreds of files simultaneously.
  - **Browser Folder Upload**: Select an entire folder directly from your browser.
  - **Local Directory Batch Queueing**: Provide a folder path on disk (e.g. `/Users/name/Videos`) with optional recursive scanning to queue all files without browser upload overhead.
  - Searchable destination selector populated directly from your Telegram dialogs (Saved Messages, Groups, Channels, Direct chats) with permanent SQLite caching.
  - Real-time WebSocket streaming: animated progress bars, live transfer speed (MB/s), ETA countdown, and chunk completion counters.
  - Per-task controls: **Pause**, **Resume**, **Retry Now**, and **Cancel**.
- **Native Android & macOS Apps**:
  - **macOS Desktop App (`Telegram Uploader.app`):** Native Swift/AppKit + WebKit wrapper in `macos/` with Menu Bar status indicator, Dock progress badge, Finder drag-and-drop & file/folder pickers, and bundled self-starting Python backend (`./macos/build_macos_app.sh`).
  - **Android Mobile App (`telegram-uploader.apk`):** Material 3 Jetpack Compose application with gallery selection, in-group duplicate checks, cache cleanup, and WorkManager background uploads.
- **Safe Upload Cache Management**:
  - One-click **🧹 Clear Cache** controls in Web Queue, Web Settings (`GET /api/cache/stats` & `POST /api/cache/clear`), macOS Menu Bar, and Android Settings.
  - Reclaims temporary staged uploads and thumbnail previews while strictly protecting active/queued/paused items, duplicate-check DB history, and external source folders.
- **100% Private & Self-Hosted**:
  - Zero telemetry, zero tracking, zero third-party clouds. Your bot tokens, sessions, databases, and media stay completely on your own hardware.
- **Dual Authentication Modes**:
  - **Telegram Bot Token** (Recommended for automation): Upload to any group or channel where your bot is added.
  - **Telegram User Account (Phone + OTP + 2FA)**: Upload directly as yourself to personal chats, private groups, and **Saved Messages**.
- **Standalone CLI Mode**:
  - Full-featured command-line interface for terminal users, background scripts, or cron jobs.

---

## 🏗️ Architecture

```
telegramuploader/
├── app/
│   ├── config.py              # Configuration, environment variables, directories
│   ├── db.py                  # SQLite schema & persistent chunk tracking (aiosqlite)
│   ├── telegram_client.py     # Telegram MTProto client manager (Telethon)
│   ├── resumable_uploader.py  # Stateful chunked uploader engine (SaveBigFilePart/SaveFilePart)
│   ├── queue_manager.py       # Background worker loop & auto-resume logic
│   ├── main.py                # FastAPI REST API & WebSocket endpoints
│   └── static/
│       ├── index.html         # Modern web application interface
│       ├── css/style.css      # Dark-mode dashboard styling
│       └── js/app.js          # Reactive WebSocket progress handler
├── tests/
│   ├── test_uploader.py       # Unit tests for chunk resume & state recovery
│   ├── test_reconnect_recovery.py # Network disconnect & missing-part repair tests
│   ├── test_cache.py          # Cache stats & protected-file cleanup tests
│   └── test_api_and_queue.py  # Integration tests for API endpoints & queue actions
├── macos/                     # Native macOS desktop app builder (Swift + WebKit)
├── android/                   # Native Android Jetpack Compose app
├── cli.py                     # Standalone CLI tool
├── run.sh                     # One-click startup script
└── requirements.txt           # Python dependencies
```

---

## 🚀 Quick Start

### 1. Requirements
- Python 3.10+
- Telegram `API_ID` and `API_HASH` (get them in 2 minutes for free at [my.telegram.org](https://my.telegram.org))
- Telegram Bot Token (from [@BotFather](https://t.me/BotFather)) OR a phone number.

### 2. Launching the Web Application

Simply run:
```bash
./run.sh
```

Or manually:
```bash
source venv/bin/activate
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

Open your browser at **`http://localhost:8080`**.

---

## ⚙️ Configuration & Telegram Setup

### In the Web UI:
1. Navigate to the **Telegram & Settings** tab.
2. Enter your **Telegram API ID** and **API Hash**.
3. Choose your login method:
   - **Bot Token**: Enter your token from `@BotFather` and click **Connect Telegram Bot**.
   - **Personal Account**: Enter your phone number (`+1...`), receive the Telegram code, enter it, and sign in.
4. Once authenticated, the header status badge turns green with your bot/user name, and your groups/channels will appear automatically in the destination dropdown.

### Via Environment Variables (Optional):
You can also set environment variables in your shell or `.env`:
```bash
export TELEGRAM_API_ID="12345678"
export TELEGRAM_API_HASH="0123456789abcdef0123456789abcdef"
export TELEGRAM_BOT_TOKEN="1234567890:ABCdefGhIJKlmNoPQRsTUVwxyZ"
```

---

## 💻 Using the Command Line Interface (CLI)

The application includes a standalone CLI `cli.py`:

### Authenticate
```bash
# Authenticate as Bot:
python cli.py auth --api-id 12345678 --api-hash your_hash --bot-token "123456:ABC-DEF..."

# Or authenticate as User:
python cli.py auth --api-id 12345678 --api-hash your_hash --phone "+1234567890"
```

### Upload a Media File or Entire Directory with Auto-Resume
```bash
# Upload a single file:
python cli.py upload /path/to/video.mp4 --chat @mychannel --caption "Here is the video"

# Upload all files in a folder (batch):
python cli.py upload /path/to/my_folder --chat @mychannel

# Upload all files including subfolders recursively:
python cli.py upload /path/to/my_folder --chat @mychannel --recursive
```

Options:
- `file`: Path to a media file OR a directory containing media.
- `--chat`: Username (`@channel`), numerical chat ID (`-100123456789`), or `me` (Saved Messages).
- `--caption`: Text caption for the media.
- `--media-type`: `auto` (default), `video`, `photo`, `audio`, or `document`.
- `--document`: Send uncompressed document preserving original file quality.
- `--recursive`, `-r`: If uploading a folder, search subdirectories recursively.
- `--concurrency`: Number of parallel chunk uploads (default: 3).

### Check Queue & Resume Interrupted Uploads
```bash
# List all uploads in the queue:
python cli.py queue

# Resume any interrupted or pending uploads:
python cli.py resume
```

---

## 🧪 Running Unit Tests

The test suite includes tests for chunk tracking, simulated network failures, state recovery, and API endpoints:

```bash
source venv/bin/activate
python3 -m unittest discover tests
```

---

## 🛡️ How Auto-Resume Works Internally

1. **Chunk Indexing**: When an upload begins, the file is split into numbered chunks ($0, 1, \dots, N-1$).
2. **Persistent State**: As each part completes over MTProto, it is recorded in `task_parts` inside SQLite (`uploader.db`).
3. **Failure Detection**: If a socket error, connection reset, or process termination occurs, the task enters `retrying` status with an exponential backoff timer.
4. **Resumption**: When retried (by the worker loop or upon application restart):
   - The uploader queries `SELECT part_index FROM task_parts WHERE task_id = ?`.
   - Any parts already uploaded are skipped.
   - Reading begins directly at the first missing chunk offset.
   - Once all parts are present, Telegram's remote handle (`InputFileBig` / `InputFile`) is finalized and delivered to the destination chat.
