import os
import asyncio
import logging
from pathlib import Path
from typing import Optional, Dict, Any, List

from telethon import TelegramClient, errors, utils
from telethon.tl.types import (
    Channel, Chat, User, InputPeerSelf,
    InputPeerChannel, InputPeerChat, InputPeerUser
)
from .config import SESSIONS_DIR, load_config, save_config, DEFAULT_API_ID, DEFAULT_API_HASH

logger = logging.getLogger("telegram_client")
logger.setLevel(logging.INFO)

SESSION_NAME = str(SESSIONS_DIR / "telegram_uploader")

class TelegramManager:
    def __init__(self):
        self.client: Optional[TelegramClient] = None
        self.phone_code_hash: Optional[str] = None
        self.pending_phone: Optional[str] = None
        self._lock = asyncio.Lock()
        self._auth_cache: Dict[str, Any] = {
            "is_connected": False,
            "is_authorized": False,
            "user": None,
            "error": None
        }

    def _build_client(self, api_id: int, api_hash: str) -> TelegramClient:
        """Creates a configured Telethon client with resilient timeouts and retry settings."""
        return TelegramClient(
            SESSION_NAME,
            api_id=int(api_id),
            api_hash=str(api_hash),
            timeout=20,
            request_retries=5,
            connection_retries=10,
            retry_delay=2,
            auto_reconnect=True
        )

    async def get_client(self) -> Optional[TelegramClient]:
        if self.client and self.client.is_connected():
            return self.client
        return await self.initialize()

    async def reconnect(self) -> Optional[TelegramClient]:
        """Forces a clean disconnect and fresh MTProto socket reconnect after network outages."""
        async with self._lock:
            if self.client:
                try:
                    await asyncio.wait_for(self.client.disconnect(), timeout=5.0)
                except Exception as e:
                    logger.debug(f"Ignoring disconnect error during reconnect: {e}")
                self.client = None
        return await self.initialize()

    async def initialize(self) -> Optional[TelegramClient]:
        async with self._lock:
            cfg = load_config()
            api_id = cfg.get("api_id") or DEFAULT_API_ID
            api_hash = cfg.get("api_hash") or DEFAULT_API_HASH

            try:
                if self.client is None:
                    self.client = self._build_client(int(api_id), str(api_hash))

                if not self.client.is_connected():
                    await asyncio.wait_for(self.client.connect(), timeout=20.0)

                is_auth = await asyncio.wait_for(self.client.is_user_authorized(), timeout=15.0)
                if is_auth:
                    me = await asyncio.wait_for(self.client.get_me(), timeout=15.0)
                    self._auth_cache = {
                        "is_connected": True,
                        "is_authorized": True,
                        "user": {
                            "id": me.id,
                            "first_name": me.first_name,
                            "last_name": me.last_name,
                            "username": me.username,
                            "phone": getattr(me, "phone", None),
                            "is_bot": getattr(me, "bot", False)
                        },
                        "error": None
                    }
                else:
                    # If bot token is configured, try auto-login
                    bot_token = cfg.get("bot_token")
                    if bot_token:
                        try:
                            await asyncio.wait_for(self.client.start(bot_token=bot_token), timeout=20.0)
                            me = await asyncio.wait_for(self.client.get_me(), timeout=15.0)
                            self._auth_cache = {
                                "is_connected": True,
                                "is_authorized": True,
                                "user": {
                                    "id": me.id,
                                    "first_name": me.first_name,
                                    "last_name": me.last_name,
                                    "username": me.username,
                                    "phone": None,
                                    "is_bot": True
                                },
                                "error": None
                            }
                        except Exception as e:
                            self._auth_cache = {
                                "is_connected": True,
                                "is_authorized": False,
                                "user": None,
                                "error": f"Bot login failed: {str(e)}"
                            }
                    else:
                        self._auth_cache = {
                            "is_connected": True,
                            "is_authorized": False,
                            "user": None,
                            "error": "Not logged in. Please sign in via Bot Token or Phone."
                        }
                return self.client
            except Exception as e:
                logger.error(f"Failed to initialize TelegramClient: {e}")
                self._auth_cache = {
                    "is_connected": False,
                    "is_authorized": False,
                    "user": None,
                    "error": str(e)
                }
                return None

    async def get_status(self) -> Dict[str, Any]:
        """Returns cached authentication & connection status and keeps worker alive."""
        # Self-heal background upload worker if it ever stopped unexpectedly
        try:
            from .queue_manager import queue_manager
            queue_manager.ensure_worker_alive()
        except Exception:
            pass

        if self.client and self.client.is_connected():
            try:
                is_auth = await asyncio.wait_for(self.client.is_user_authorized(), timeout=5.0)
                if is_auth and not self._auth_cache.get("is_authorized"):
                    me = await asyncio.wait_for(self.client.get_me(), timeout=5.0)
                    self._auth_cache["is_authorized"] = True
                    self._auth_cache["user"] = {
                        "id": me.id,
                        "first_name": me.first_name,
                        "last_name": me.last_name,
                        "username": me.username,
                        "phone": getattr(me, "phone", None),
                        "is_bot": getattr(me, "bot", False)
                    }
                self._auth_cache["is_connected"] = True
                self._auth_cache["error"] = None
            except Exception as e:
                self._auth_cache["is_connected"] = False
                self._auth_cache["error"] = str(e)
        else:
            self._auth_cache["is_connected"] = False
        return self._auth_cache

    async def login_bot(
        self,
        bot_token: str,
        api_id: Optional[int] = None,
        api_hash: Optional[str] = None
    ) -> Dict[str, Any]:
        """Authenticates with a Telegram Bot Token."""
        async with self._lock:
            # 1. Pre-validate token against Telegram Bot API for friendly feedback
            try:
                import requests
                chk = await asyncio.to_thread(requests.get, f"https://api.telegram.org/bot{bot_token}/getMe", timeout=5)
                if chk.status_code == 429:
                    c_data = chk.json()
                    retry_sec = c_data.get("parameters", {}).get("retry_after", 30)
                    raise ValueError(
                        f"This Bot Token is temporarily locked by Telegram (429 Rate Limit: retry after {retry_sec}s). "
                        "Fix: Go to @BotFather in Telegram, send /revoke, select this bot, and paste the new token!"
                    )
                elif chk.status_code == 401:
                    raise ValueError("Invalid Bot Token. Please make sure you copied the full token from @BotFather.")
            except (ValueError, RuntimeError):
                raise
            except Exception as net_err:
                logger.warning(f"Bot token HTTP pre-check warning: {net_err}")

            effective_api_id = int(api_id or DEFAULT_API_ID)
            effective_api_hash = str(api_hash or DEFAULT_API_HASH)

            if self.client:
                try:
                    await self.client.disconnect()
                except Exception:
                    pass

            # Clear any stale session files
            for p in SESSIONS_DIR.glob("telegram_uploader*"):
                try:
                    p.unlink()
                except Exception:
                    pass

            # Save credentials
            save_config({
                "api_id": effective_api_id,
                "api_hash": effective_api_hash,
                "bot_token": bot_token,
                "auth_mode": "bot"
            })

            try:
                self.client = self._build_client(effective_api_id, effective_api_hash)
                await self.client.connect()
                await self.client.sign_in(bot_token=bot_token)
                me = await self.client.get_me()
            except errors.RpcCallFailError:
                raise ValueError(
                    "Telegram servers reported an internal issue authorizing this bot. "
                    "This happens when Telegram flood-protects a new bot. Fix: Send /revoke to @BotFather to generate a clean token."
                )
            except Exception as e:
                err_str = str(e)
                if "unsuccessful" in err_str.lower():
                    raise ValueError(
                        "Telegram rejected the bot login (Request was unsuccessful). "
                        "Telegram locks tokens that were queried too frequently. Fix: Send /revoke to @BotFather to get a fresh token."
                    )
                raise

            self._auth_cache = {
                "is_connected": True,
                "is_authorized": True,
                "user": {
                    "id": me.id,
                    "first_name": me.first_name,
                    "last_name": me.last_name,
                    "username": me.username,
                    "phone": None,
                    "is_bot": True
                },
                "error": None
            }
            return self._auth_cache

    async def send_phone_code(self, api_id: int, api_hash: str, phone: str) -> Dict[str, Any]:
        """Initiates User Phone Login."""
        async with self._lock:
            if self.client:
                try:
                    await self.client.disconnect()
                except Exception:
                    pass

            save_config({
                "api_id": api_id,
                "api_hash": api_hash,
                "phone": phone,
                "auth_mode": "user"
            })

            self.client = self._build_client(api_id, api_hash)
            await self.client.connect()
            res = await self.client.send_code_request(phone)
            self.phone_code_hash = res.phone_code_hash
            self.pending_phone = phone
            return {
                "success": True,
                "phone": phone,
                "phone_code_hash": self.phone_code_hash,
                "message": f"Verification code sent to {phone}."
            }

    async def sign_in_with_code(self, code: str, password: Optional[str] = None) -> Dict[str, Any]:
        """Completes phone login using code and optional 2FA password."""
        async with self._lock:
            if not self.client or not self.client.is_connected():
                raise RuntimeError("Telegram client is not connected.")
            if not self.pending_phone or not self.phone_code_hash:
                raise RuntimeError("No phone verification in progress. Please request a code first.")

            try:
                await self.client.sign_in(
                    phone=self.pending_phone,
                    code=code,
                    phone_code_hash=self.phone_code_hash
                )
            except errors.SessionPasswordNeededError:
                if not password:
                    return {
                        "requires_2fa": True,
                        "message": "Two-step verification (2FA) password required."
                    }
                await self.client.sign_in(password=password)

            me = await self.client.get_me()
            self._auth_cache = {
                "is_connected": True,
                "is_authorized": True,
                "user": {
                    "id": me.id,
                    "first_name": me.first_name,
                    "last_name": me.last_name,
                    "username": me.username,
                    "phone": getattr(me, "phone", None),
                    "is_bot": False
                },
                "error": None
            }
            self.pending_phone = None
            self.phone_code_hash = None
            return self._auth_cache

    async def logout(self) -> bool:
        """Logs out from Telegram and clears the session."""
        async with self._lock:
            if self.client:
                try:
                    if await self.client.is_user_authorized():
                        await self.client.log_out()
                    else:
                        await self.client.disconnect()
                except Exception as e:
                    logger.warning(f"Error during logout: {e}")
                self.client = None

            # Remove session files
            for p in SESSIONS_DIR.glob("telegram_uploader*"):
                try:
                    p.unlink()
                except Exception:
                    pass

            self._auth_cache = {
                "is_connected": False,
                "is_authorized": False,
                "user": None,
                "error": None
            }
            return True

    async def get_bot_dialogs(self) -> List[Dict[str, Any]]:
        """
        Discovers chats, groups, and channels for a bot:
        1. Loads all persistent known chats from database.
        2. Queries getUpdates to catch any newly joined groups / chats.
        3. For each group, refreshes and validates title/type via getChat API.
        4. Permanently caches discovered dialogs in SQLite so they never disappear.
        """
        from . import db
        cfg = load_config()
        token = cfg.get("bot_token")

        dialogs = []
        seen_ids = set()

        # 1. Start with persistent known dialogs from SQLite
        try:
            known = await db.get_known_dialogs()
            for k in known:
                cid = str(k["id"])
                if cid not in seen_ids:
                    seen_ids.add(cid)
                    dialogs.append({
                        "id": k["id"],
                        "title": k["title"],
                        "username": k.get("username"),
                        "type": k.get("type", "group"),
                        "unread_count": 0
                    })
        except Exception as e:
            logger.warning(f"Error loading known dialogs from DB: {e}")

        # 2. Check getUpdates for any newly joined chats
        if token:
            try:
                import requests
                res = await asyncio.to_thread(requests.get, f"https://api.telegram.org/bot{token}/getUpdates", timeout=5)
                if res.ok:
                    data = res.json().get("result", [])
                    for item in data:
                        msg = (
                            item.get("message")
                            or item.get("channel_post")
                            or item.get("my_chat_member")
                            or item.get("edited_message")
                        )
                        if not msg:
                            continue
                        chat = msg.get("chat")
                        if not chat:
                            continue
                        cid = str(chat["id"])
                        if cid in seen_ids:
                            continue
                        seen_ids.add(cid)
                        c_type = chat.get("type", "group")
                        title = chat.get("title") or chat.get("username") or chat.get("first_name") or cid
                        d_type = "channel" if c_type == "channel" else ("group" if "group" in c_type else "user")
                        dialog_obj = {
                            "id": chat["id"],
                            "title": title,
                            "username": chat.get("username"),
                            "type": d_type,
                            "unread_count": 0
                        }
                        dialogs.append(dialog_obj)
                        await db.save_known_dialog(cid, title, chat.get("username"), d_type)
            except Exception as e:
                logger.warning(f"Error fetching bot updates: {e}")

        # 3. Refresh live title & details for any dialog that has token access
        if token and dialogs:
            try:
                import requests
                for d in dialogs:
                    cid = d["id"]
                    try:
                        res = await asyncio.to_thread(requests.get, f"https://api.telegram.org/bot{token}/getChat?chat_id={cid}", timeout=4)
                        if res.ok:
                            cdata = res.json().get("result", {})
                            rtitle = cdata.get("title") or cdata.get("first_name") or cdata.get("username")
                            if rtitle:
                                d["title"] = rtitle
                                rtype = cdata.get("type", d.get("type", "group"))
                                d["type"] = "channel" if rtype == "channel" else ("group" if "group" in rtype else "user")
                                d["username"] = cdata.get("username") or d.get("username")
                                await db.save_known_dialog(str(cid), d["title"], d["username"], d["type"])
                    except Exception:
                        pass
            except Exception as e:
                logger.debug(f"Error refreshing getChat: {e}")

        return dialogs

    async def get_dialogs(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Retrieves list of chats, channels, groups, and Saved Messages."""
        client = await self.get_client()
        if not client or not await client.is_user_authorized():
            from . import db
            return await db.get_known_dialogs()

        try:
            me = await client.get_me()
            # Telegram restricts messages.getDialogs for bot accounts. Use get_bot_dialogs for bots.
            if getattr(me, "bot", False):
                return await self.get_bot_dialogs()

            dialog_list = [
                {
                    "id": "me",
                    "title": "Saved Messages (Me)",
                    "username": me.username or "me",
                    "type": "saved",
                    "unread_count": 0
                }
            ]

            async for dialog in client.iter_dialogs(limit=limit):
                entity = dialog.entity
                d_type = "user"
                if isinstance(entity, Channel):
                    d_type = "channel" if getattr(entity, "broadcast", False) else "group"
                elif isinstance(entity, Chat):
                    d_type = "group"
                elif isinstance(entity, User):
                    d_type = "user"

                if entity.id == me.id:
                    continue

                username = getattr(entity, "username", None)
                dialog_list.append({
                    "id": entity.id,
                    "title": dialog.name or (f"@{username}" if username else str(entity.id)),
                    "username": username,
                    "type": d_type,
                    "unread_count": dialog.unread_count
                })
            return dialog_list
        except Exception as e:
            logger.error(f"Failed to fetch dialogs: {e}")
            from . import db
            return await db.get_known_dialogs()

    async def resolve_target(self, target: Any) -> Any:
        """Resolves target chat string, int, or username to a valid input entity."""
        client = await self.get_client()
        if not client:
            raise RuntimeError("Telegram client not available.")

        if target in ("me", "self", "Saved Messages"):
            return await client.get_input_entity("me")

        # Check if integer ID string
        try:
            int_id = int(target)
            return await client.get_input_entity(int_id)
        except (ValueError, TypeError):
            pass

        # Check known dialogs by title match
        try:
            dialogs = await self.get_dialogs()
            target_str = str(target).strip().lower()
            for d in dialogs:
                if d.get("title", "").strip().lower() == target_str or (d.get("username") and d["username"].lower() == target_str.lstrip("@")):
                    return await client.get_input_entity(int(d["id"]))
        except Exception:
            pass

        # String username or link
        try:
            return await client.get_input_entity(target)
        except Exception as e:
            me = await client.get_me()
            bot_hint = f" or add @{me.username} to the group and send a message so it can discover it" if getattr(me, "bot", False) else ""
            raise ValueError(
                f"Could not find Telegram chat '{target}'. If this is a private group or channel, "
                f"please use its numerical Chat ID (e.g. -100...){bot_hint}."
            ) from e

    async def scan_group_media(self, target: Any, limit: int = 500) -> Dict[str, Any]:
        """
        Scans a Telegram group or channel for media files (documents, videos, audio, photos)
        to identify existing files and detect internal duplicates.
        """
        client = await self.get_client()
        if not client or not await client.is_user_authorized():
            raise RuntimeError("Telegram client not connected.")

        entity = await self.resolve_target(target)
        me = await client.get_me()

        media_items = []
        error_notice = None

        try:
            async for msg in client.iter_messages(entity, limit=limit):
                if not msg.media:
                    continue

                file_name = None
                file_size = 0
                mime_type = None
                media_type = "unknown"

                if msg.file:
                    file_name = msg.file.name
                    file_size = msg.file.size or 0
                    mime_type = msg.file.mime_type
                    if msg.video:
                        media_type = "video"
                    elif msg.audio or msg.voice:
                        media_type = "audio"
                    elif msg.photo:
                        media_type = "photo"
                    elif msg.document:
                        media_type = "document"

                if not file_name and msg.photo:
                    media_type = "photo"
                    file_name = f"photo_{msg.id}.jpg"
                    if hasattr(msg.photo, "sizes") and msg.photo.sizes:
                        file_size = getattr(msg.photo.sizes[-1], "size", 0)

                if file_name or file_size > 0:
                    media_items.append({
                        "message_id": msg.id,
                        "date": msg.date.isoformat() if msg.date else None,
                        "file_name": file_name,
                        "file_size": file_size,
                        "media_type": media_type,
                        "mime_type": mime_type,
                        "caption": msg.text or "",
                        "sender_id": msg.sender_id,
                        "is_by_me": msg.sender_id == me.id
                    })
        except Exception as e:
            logger.warning(f"Could not fetch full group messages directly via MTProto: {e}")
            error_notice = str(e)

        return {
            "media_items": media_items,
            "error_notice": error_notice
        }

telegram_manager = TelegramManager()
