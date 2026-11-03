import time
import logging
import asyncio
import functools
import inspect
import re
from hashlib import sha256
from typing import Callable, Optional, AsyncGenerator, Union

from pyrogram import Client, raw, utils
from pyrogram.types import Message
from pyrogram.session.auth import Auth
from pyrogram.session import Session
from pyrogram.file_id import FileId, FileType, ThumbnailSource
from pyrogram.errors import VolumeLocNotFound, CDNFileHashMismatch, FloodWait
from pyrogram.crypto import aes
import pyrogram
from config import Config
from utils import parse_split_info

logger = logging.getLogger("tg_client")

# Monkey-patch to cache auth keys across media sessions
_original_auth_create = Auth.create
_auth_key_cache = {}

async def _patched_auth_create(self):
    if self.dc_id in _auth_key_cache:
        logger.info(f"Reusing cached auth key for DC{self.dc_id}")
        return _auth_key_cache[self.dc_id]
    
    logger.info(f"Generating new auth key for DC{self.dc_id}...")
    key = await _original_auth_create(self)
    _auth_key_cache[self.dc_id] = key
    return key

Auth.create = _patched_auth_create

# Monkey-patch Pyrogram for 64-bit Telegram channel IDs (e.g. -1003905555685)
# Pyrogram has an outdated 32-bit MIN_CHANNEL_ID (-1002147483647).
# Modern Telegram channel IDs exceed 2147483647, causing "Peer id invalid".
utils.MIN_CHANNEL_ID = -100999999999999
pyrogram.utils.MIN_CHANNEL_ID = -100999999999999

_orig_get_peer_type = utils.get_peer_type
def _safe_get_peer_type(peer_id: int) -> str:
    if isinstance(peer_id, int) and peer_id < 0 and peer_id < utils.MAX_CHANNEL_ID:
        return "channel"
    return _orig_get_peer_type(peer_id)

utils.get_peer_type = _safe_get_peer_type
pyrogram.utils.get_peer_type = _safe_get_peer_type


class MediaSessionPool:
    """Pool of parallel MTProto media sessions for a specific DC.
    
    Provides non-blocking dynamic leasing via asyncio.Queue so seeking
    and concurrent requests never block or deadlock each other.
    """
    def __init__(self, client: Client, dc_id: int, pool_size: int = 4):
        self.client = client
        self.dc_id = dc_id
        self.pool_size = pool_size
        self._queue: asyncio.Queue[Session] = asyncio.Queue()
        self._all_sessions: list[Session] = []
        self._lock = asyncio.Lock()
        self._initialized = False

    async def ensure_initialized(self):
        if self._initialized and len(self._all_sessions) >= self.pool_size:
            return
        async with self._lock:
            if self._initialized and len(self._all_sessions) >= self.pool_size:
                return

            auth_key = (
                await Auth(self.client, self.dc_id, await self.client.storage.test_mode()).create()
                if self.dc_id != await self.client.storage.dc_id()
                else await self.client.storage.auth_key()
            )
            exported_auth = None
            if self.dc_id != await self.client.storage.dc_id():
                exported_auth = await self.client.invoke(
                    raw.functions.auth.ExportAuthorization(dc_id=self.dc_id)
                )

            while len(self._all_sessions) < self.pool_size:
                idx = len(self._all_sessions) + 1
                logger.info(f"Creating parallel media session {idx}/{self.pool_size} for DC{self.dc_id}...")
                session = Session(
                    self.client,
                    self.dc_id,
                    auth_key,
                    await self.client.storage.test_mode(),
                    is_media=True
                )
                await session.start()

                if exported_auth is not None:
                    await session.invoke(
                        raw.functions.auth.ImportAuthorization(
                            id=exported_auth.id,
                            bytes=exported_auth.bytes
                        )
                    )
                self._all_sessions.append(session)
                self._queue.put_nowait(session)
            self._initialized = True

    async def acquire(self) -> Session:
        await self.ensure_initialized()
        return await self._queue.get()

    def release(self, session: Session):
        self._queue.put_nowait(session)

    async def restart_session(self, session: Session):
        try:
            await session.stop()
        except Exception:
            pass
        try:
            await session.start()
            if self.dc_id != await self.client.storage.dc_id():
                exported_auth = await self.client.invoke(
                    raw.functions.auth.ExportAuthorization(dc_id=self.dc_id)
                )
                await session.invoke(
                    raw.functions.auth.ImportAuthorization(
                        id=exported_auth.id,
                        bytes=exported_auth.bytes
                    )
                )
        except Exception as e:
            logger.warning(f"Error restarting media session for DC{self.dc_id}: {e}")

    async def stop(self):
        async with self._lock:
            for s in self._all_sessions:
                try:
                    await s.stop()
                except Exception:
                    pass
            self._all_sessions.clear()
            while not self._queue.empty():
                try:
                    self._queue.get_nowait()
                except Exception:
                    break
            self._initialized = False


async def fetch_single_chunk(
    pool: MediaSessionPool,
    location: raw.base.InputFileLocation,
    off_bytes: int,
    chunk_size: int
):
    offset_mb = off_bytes / (1024 * 1024)
    query = raw.functions.upload.GetFile(
        location=location,
        offset=off_bytes,
        limit=chunk_size
    )
    retries = 3
    for attempt in range(retries):
        session = None
        try:
            session = await pool.acquire()
            r = await session.invoke(query, sleep_threshold=0)
            return r
        except FloodWait as e:
            logger.warning(
                f"🚨 [FLOOD WAIT] Telegram rate limit active on DC{pool.dc_id}! "
                f"Pausing for {e.value} seconds (offset {offset_mb:.1f} MB)..."
            )
            await asyncio.sleep(e.value)
        except (asyncio.CancelledError, pyrogram.StopTransmission):
            raise
        except (OSError, TimeoutError) as e:
            if session is not None:
                await pool.restart_session(session)
            if attempt == retries - 1:
                logger.error(f"Network error fetching chunk at {offset_mb:.1f} MB: {e}")
                raise
            logger.warning(f"Network error at {offset_mb:.1f} MB: {e}. Retrying ({attempt+1}/{retries})...")
            await asyncio.sleep(0.3)
        except Exception as e:
            if attempt == retries - 1:
                logger.error(f"Error fetching chunk at {offset_mb:.1f} MB: {e}")
                raise
            logger.warning(f"Error fetching chunk at {offset_mb:.1f} MB: {e}. Retrying ({attempt+1}/{retries})...")
            await asyncio.sleep(0.3)
        finally:
            if session is not None:
                pool.release(session)


# Monkey-patch Client.get_file with pipelined parallel downloading and instant-seek prefetching
async def _patched_get_file(
    self: Client,
    file_id: FileId,
    file_size: int = 0,
    limit: int = 0,
    offset: int = 0,
    progress: Callable = None,
    progress_args: tuple = ()
) -> Optional[AsyncGenerator[bytes, None]]:
    async with self.get_file_semaphore:
        file_type = file_id.file_type

        if file_type == FileType.CHAT_PHOTO:
            if file_id.chat_id > 0:
                peer = raw.types.InputPeerUser(
                    user_id=file_id.chat_id,
                    access_hash=file_id.chat_access_hash
                )
            else:
                if file_id.chat_access_hash == 0:
                    peer = raw.types.InputPeerChat(
                        chat_id=-file_id.chat_id
                    )
                else:
                    peer = raw.types.InputPeerChannel(
                        channel_id=utils.get_channel_id(file_id.chat_id),
                        access_hash=file_id.chat_access_hash
                    )

            location = raw.types.InputPeerPhotoFileLocation(
                peer=peer,
                photo_id=file_id.media_id,
                big=file_id.thumbnail_source == ThumbnailSource.CHAT_PHOTO_BIG
            )
        elif file_type == FileType.PHOTO:
            location = raw.types.InputPhotoFileLocation(
                id=file_id.media_id,
                access_hash=file_id.access_hash,
                file_reference=file_id.file_reference,
                thumb_size=file_id.thumbnail_size
            )
        else:
            location = raw.types.InputDocumentFileLocation(
                id=file_id.media_id,
                access_hash=file_id.access_hash,
                file_reference=file_id.file_reference,
                thumb_size=file_id.thumbnail_size
            )

        chunk_size = 1024 * 1024  # 1 MB chunk size for maximum throughput
        start_chunk = abs(offset)

        if limit:
            total_chunks = abs(limit)
        elif file_size > 0:
            total_chunks = max(1, (file_size + chunk_size - 1) // chunk_size - start_chunk)
        else:
            total_chunks = (1 << 31) - 1

        end_chunk = start_chunk + total_chunks
        dc_id = file_id.dc_id

        # Initialize custom session pool dictionary if not present
        if not hasattr(self, "_custom_media_pools"):
            self._custom_media_pools = {}
            self._custom_pools_lock = asyncio.Lock()

        pool_size = max(2, min(getattr(Config, "PARALLEL_CONNECTIONS", 4), 16))
        buffer_capacity = max(pool_size * 3, getattr(Config, "BUFFER_CHUNKS", 16))

        async with self._custom_pools_lock:
            if dc_id not in self._custom_media_pools:
                self._custom_media_pools[dc_id] = MediaSessionPool(self, dc_id, pool_size=pool_size)
            pool = self._custom_media_pools[dc_id]

        in_flight: dict[int, asyncio.Task] = {}
        next_fetch_idx = start_chunk
        next_yield_idx = start_chunk
        current = 0
        total = total_chunks

        def schedule_downloads():
            nonlocal next_fetch_idx
            while next_fetch_idx < end_chunk and len(in_flight) < buffer_capacity:
                idx = next_fetch_idx
                off_bytes = idx * chunk_size
                in_flight[idx] = asyncio.create_task(
                    fetch_single_chunk(pool, location, off_bytes, chunk_size)
                )
                next_fetch_idx += 1

        try:
            # Immediately start fetching chunks in parallel across all sessions
            schedule_downloads()

            while next_yield_idx < end_chunk:
                schedule_downloads()

                if next_yield_idx not in in_flight:
                    break

                task = in_flight.pop(next_yield_idx)
                try:
                    r = await task
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error(f"Error retrieving chunk {next_yield_idx}: {e}")
                    raise

                if isinstance(r, raw.types.upload.File):
                    chunk = r.bytes
                    yield chunk

                    current += 1
                    if progress:
                        offset_bytes = (next_yield_idx + 1) * chunk_size
                        func = functools.partial(
                            progress,
                            min(offset_bytes, file_size) if file_size != 0 else offset_bytes,
                            file_size,
                            *progress_args
                        )
                        if inspect.iscoroutinefunction(progress):
                            await func()
                        else:
                            await self.loop.run_in_executor(self.executor, func)

                    if len(chunk) < chunk_size or current >= total:
                        break

                    next_yield_idx += 1

                elif isinstance(r, raw.types.upload.FileCdnRedirect):
                    cdn_session = Session(
                        self, r.dc_id, await Auth(self, r.dc_id, await self.storage.test_mode()).create(),
                        await self.storage.test_mode(), is_media=True, is_cdn=True
                    )
                    try:
                        await cdn_session.start()
                        cdn_offset_bytes = next_yield_idx * chunk_size
                        while True:
                            r2 = await cdn_session.invoke(
                                raw.functions.upload.GetCdnFile(
                                    file_token=r.file_token,
                                    offset=cdn_offset_bytes,
                                    limit=chunk_size
                                )
                            )
                            if isinstance(r2, raw.types.upload.CdnFileReuploadNeeded):
                                session0 = await pool.acquire()
                                try:
                                    await session0.invoke(
                                        raw.functions.upload.ReuploadCdnFile(
                                            file_token=r.file_token,
                                            request_token=r2.request_token
                                        )
                                    )
                                except VolumeLocNotFound:
                                    break
                                else:
                                    continue
                                finally:
                                    pool.release(session0)

                            chunk = r2.bytes
                            decrypted_chunk = aes.ctr256_decrypt(
                                chunk,
                                r.encryption_key,
                                bytearray(
                                    r.encryption_iv[:-4]
                                    + (cdn_offset_bytes // 16).to_bytes(4, "big")
                                )
                            )
                            session0 = await pool.acquire()
                            try:
                                hashes = await session0.invoke(
                                    raw.functions.upload.GetCdnFileHashes(
                                        file_token=r.file_token,
                                        offset=cdn_offset_bytes
                                    )
                                )
                            finally:
                                pool.release(session0)

                            for i, h in enumerate(hashes):
                                cdn_chunk = decrypted_chunk[h.limit * i: h.limit * (i + 1)]
                                CDNFileHashMismatch.check(
                                    h.hash == sha256(cdn_chunk).digest(),
                                    "h.hash == sha256(cdn_chunk).digest()"
                                )

                            yield decrypted_chunk
                            current += 1
                            cdn_offset_bytes += chunk_size

                            if len(chunk) < chunk_size or current >= total:
                                break
                    finally:
                        await cdn_session.stop()
                    break
                else:
                    break
        finally:
            for task in in_flight.values():
                if not task.done():
                    task.cancel()

Client.get_file = _patched_get_file


class TelegramClientManager:
    def __init__(self):
        self.client = None
        self.is_running = False
        self._search_cache = {}
        self._message_cache = {}
        self._log_cache = {}

    def initialize(self):
        Config.validate()
        
        client_kwargs = {
            "api_id": Config.API_ID,
            "api_hash": Config.API_HASH,
            "device_model": "stremio",
            "app_version": "telegram stremio",
            "system_version": "Linux",
            "in_memory": True,
            "no_updates": True,
        }
        # Check if the installed Pyrogram fork supports max_concurrent_transmissions
        if "max_concurrent_transmissions" in inspect.signature(Client.__init__).parameters:
            client_kwargs["max_concurrent_transmissions"] = 20

        if Config.USER_SESSION_STRING:
            logger.info("Initializing User Client...")
            self.client = Client(
                name="tg_stremio_user",
                session_string=Config.USER_SESSION_STRING,
                **client_kwargs
            )
        elif Config.BOT_TOKEN:
            logger.info("Initializing Bot Client...")
            self.client = Client(
                name="tg_stremio_bot",
                bot_token=Config.BOT_TOKEN,
                **client_kwargs
            )
        else:
            raise ValueError("Neither USER_SESSION_STRING nor BOT_TOKEN is configured!")

    def get_channel_ids(self) -> list:
        val = Config.TELEGRAM_CHANNEL_ID
        if not val:
            return []
        if isinstance(val, int):
            return [val]
        parts = [p.strip() for p in str(val).split(",")]
        ids = []
        for p in parts:
            if p.startswith("-") or p.isdigit():
                try:
                    ids.append(int(p))
                except ValueError:
                    ids.append(p)
            else:
                ids.append(p)
        return ids

    async def start(self):
        if not self.client:
            self.initialize()
        
        if not self.is_running:
            logger.info("Starting Pyrogram client...")
            try:
                is_authorized = await self.client.connect()
            except Exception as e:
                try:
                    await self.client.disconnect()
                except Exception:
                    pass
                if "struct" in str(e) or "unpack" in str(e) or "binascii" in str(e):
                    raise RuntimeError(
                        "Telegram session string is malformed or invalid! "
                        "The USER_SESSION_STRING you pasted into your GitHub repository secrets is corrupted or incomplete. "
                        "Please regenerate it and copy-paste it carefully."
                    ) from e
                raise e

            if not is_authorized:
                try:
                    await self.client.disconnect()
                except Exception:
                    pass
                raise RuntimeError(
                    "Telegram client is not authorized! "
                    "Your USER_SESSION_STRING or BOT_TOKEN is invalid, expired, or missing. "
                    "Please check your GitHub repository secrets / environment variables."
                )
            self.is_running = True
            
            # Pre-cache user dialogs to populate Pyrogram's in-memory peer table
            # This prevents PeerIdInvalid errors when streaming from channels found in global search
            if Config.USER_SESSION_STRING:
                try:
                    logger.info("Pre-caching user dialogs and channel access hashes...")
                    async for _ in self.client.get_dialogs(limit=300):
                        pass
                    logger.info("User dialogs cached successfully.")
                except Exception as e:
                    logger.warning(f"Failed to pre-cache dialogs: {e}")
            
            # Resolve target channels if configured
            try:
                chat_ids = self.get_channel_ids()
                for chat_id in chat_ids:
                    try:
                        await self.client.get_chat(chat_id)
                    except Exception as e:
                        logger.warning(f"Failed to cache channel {chat_id}: {e}")
                        
                if Config.LOG_CHANNEL_ID:
                    try:
                        await self.client.get_chat(Config.LOG_CHANNEL_ID)
                    except Exception as e:
                        logger.warning(f"Failed to cache log channel {Config.LOG_CHANNEL_ID}: {e}")
            except Exception as e:
                logger.warning(f"Failed to resolve target channels on startup: {e}")
            
            logger.info("Using Telegram global search across all channels in the account.")

    async def stop(self):
        if self.is_running and self.client:
            logger.info("Stopping Pyrogram client...")
            try:
                if hasattr(self.client, "_custom_media_pools"):
                    for pool in list(self.client._custom_media_pools.values()):
                        try:
                            await pool.stop()
                        except Exception:
                            pass
                    self.client._custom_media_pools.clear()
            except Exception:
                pass
            try:
                await asyncio.wait_for(self.client.stop(), timeout=3.0)
            except asyncio.TimeoutError:
                logger.warning("Pyrogram client stop timed out, skipping...")
            except Exception as e:
                logger.warning(f"Error stopping Pyrogram client: {e}")
            self.is_running = False

    async def send_play_log(self, filename: str, chat_id: Union[str, int], message_id: int):
        if not Config.LOG_CHANNEL_ID:
            return
            
        key = (chat_id, message_id)
        now = time.time()
        
        # Avoid duplicate logs for the same file within 15 mins
        if key in self._log_cache and now - self._log_cache[key] < 900:
            return
                
        self._log_cache[key] = now
        
        try:
            import datetime
            from datetime import timezone, timedelta
            
            tz_str = getattr(Config, "TIMEZONE", "UTC") or "UTC"
            local_dt = None
            
            try:
                from zoneinfo import ZoneInfo
                local_dt = datetime.datetime.now(ZoneInfo(tz_str))
            except Exception:
                pass
                
            if local_dt is None:
                try:
                    tz_clean = tz_str.upper().replace("UTC", "").replace("GMT", "").strip()
                    if tz_clean and tz_clean[0] in ("+", "-"):
                        sign = 1 if tz_clean[0] == "+" else -1
                        time_parts = tz_clean[1:].split(":")
                        hours = int(time_parts[0])
                        minutes = int(time_parts[1]) if len(time_parts) > 1 else 0
                        td = timedelta(hours=hours, minutes=minutes)
                        local_dt = datetime.datetime.now(timezone(sign * td))
                except Exception:
                    pass
            
            if local_dt is None:
                local_dt = datetime.datetime.now(timezone.utc)
                
            time_str = local_dt.strftime("%Y-%m-%d %H:%M:%S")
            year_str = local_dt.strftime("%Y")
            
            message_text = (
                f"🎬 **Media Stream Log**\n\n"
                f"📁 **File Name:** `{filename}`\n"
                f"📅 **Date & Time:** `{time_str}`\n"
                f"📆 **Year:** `{year_str}`\n"
                f"💬 **Source Channel:** `{chat_id}`\n"
                f"🆔 **Message ID:** `{message_id}`"
            )
            
            await self.client.send_message(
                chat_id=Config.LOG_CHANNEL_ID,
                text=message_text
            )
        except Exception as e:
            logger.error(f"Failed to send log to log channel: {e}")

    async def search_messages(self, query: str = "", limit: int = 50):
        if not self.is_running:
            await self.start()
        
        query_str = str(query).strip() if query else ""
        
        cache_key = f"{query_str}:{limit}"
        now = time.time()
        if cache_key in self._search_cache:
            cached_time, cached_results = self._search_cache[cache_key]
            if now - cached_time < Config.CACHE_TTL:
                return cached_results

        results = []
        target_chat_ids = self.get_channel_ids()
        
        if query_str:
            # 1. Fast path: If target channel(s) are configured, search them first (<50ms, no global FloodWait)
            if target_chat_ids:
                for chat_id in target_chat_ids:
                    try:
                        async for msg in self.client.search_messages(chat_id=chat_id, query=query_str, limit=min(limit, 50)):
                            if self._has_media(msg):
                                results.append(msg)
                    except Exception as e:
                        logger.debug(f"Direct channel search failed for {chat_id}: {e}")

            # 2. Global search: search across account's dialogs/channels with bounded limit (max 50 to avoid 25 RPC roundtrips)
            search_limit = min(limit, 50)
            try:
                logger.info(f"Performing global search for: '{query_str}' (limit={search_limit})")
                async for msg in self.client.search_global(query=query_str, limit=search_limit):
                    if self._has_media(msg):
                        results.append(msg)
            except FloodWait as e:
                logger.warning(f"🚨 [FLOOD WAIT] Global search rate-limited by Telegram! Required wait: {e.value}s.")
            except Exception as e:
                logger.warning(f"Telegram global search failed: {e}")
        else:
            # For no-query browsing (catalog loading), fetch channel history with requested limit
            if target_chat_ids:
                fetch_limit = min(limit, 200)
                for chat_id in target_chat_ids:
                    try:
                        async for msg in self.client.get_chat_history(chat_id=chat_id, limit=fetch_limit):
                            if self._has_media(msg):
                                results.append(msg)
                    except Exception as e:
                        logger.warning(f"Telegram history fetch failed for {chat_id}: {e}")
            else:
                logger.info("No query and no channels configured, returning empty results.")
        
        results.sort(key=lambda m: m.date, reverse=True)
        
        # Only resolve missing split parts if an explicit search query was performed.
        # NEVER perform global split searches when browsing channel catalog, as all parts are already in channel history.
        additional_messages = []
        if query_str:
            known_split_keys = set()
            split_bases = {}
            for msg in results:
                media = msg.video or msg.document or msg.audio
                if media:
                    fn = getattr(media, "file_name", "") or msg.caption or ""
                    base, part = parse_split_info(fn)
                    if base:
                        key = (msg.chat.id if msg.chat else 0, base.lower())
                        known_split_keys.add(key)
                        if base.lower() not in split_bases:
                            search_query = re.sub(r'[^a-zA-Z0-9\s]', ' ', base)
                            search_query = re.sub(r'\s+', ' ', search_query).strip()
                            words = search_query.split()
                            if words and words[-1].lower() in ('mkv', 'mp4', 'avi', 'mov', 'wmv', 'flv', 'webm', 'ts', 'm4v', 'zip'):
                                words = words[:-1]
                            search_query = " ".join(words[:4]) if len(words) > 4 else " ".join(words)
                            split_bases[base.lower()] = (search_query, msg.chat.id if msg.chat else None)
                            
            # Check if any split base only has 1 part found
            for base_lower, (base_query, chat_id) in split_bases.items():
                matching_parts = [
                    m for m in results 
                    if (parse_split_info(getattr(m.video or m.document or m.audio, "file_name", "") or m.caption or "")[0] or "").lower() == base_lower
                ]
                # If only 1 part found, try finding companion parts in the same chat
                if len(matching_parts) == 1 and chat_id:
                    try:
                        async for msg in self.client.search_messages(chat_id=chat_id, query=base_query, limit=15):
                            if self._has_media(msg):
                                additional_messages.append(msg)
                    except Exception:
                        pass
                
        # Merge and deduplicate by (chat_id, message_id)
        deduped = {(msg.chat.id, msg.id): msg for msg in results}
        for msg in additional_messages:
            if msg.chat:
                deduped[(msg.chat.id, msg.id)] = msg
            
        final_results = list(deduped.values())
        final_results.sort(key=lambda m: m.date, reverse=True)
        final_results = final_results[:limit]
        
        # Pre-populate message cache with all discovered search results
        for msg in final_results:
            if msg.chat:
                self._message_cache[f"{msg.chat.id}:{msg.id}"] = (now, msg)
                try:
                    self._message_cache[f"{int(msg.chat.id)}:{int(msg.id)}"] = (now, msg)
                except Exception:
                    pass
        
        self._search_cache[cache_key] = (now, final_results)
        return final_results

    async def get_messages(self, message_ids: list, chat_id: int = None) -> list:
        """Batch fetch multiple messages in a single Telegram RPC call with cache support."""
        if not self.is_running:
            await self.start()
            
        if not message_ids:
            return []
            
        if chat_id is not None:
            target_chat = chat_id
        else:
            channel_ids = self.get_channel_ids()
            if channel_ids:
                target_chat = channel_ids[0]
            else:
                raise ValueError("chat_id is required when TELEGRAM_CHANNEL_ID is not configured")
                
        now = time.time()
        results_map = {}
        missing_ids = []
        
        for m_id in message_ids:
            cache_key = f"{target_chat}:{m_id}"
            if cache_key in self._message_cache:
                cached_time, cached_msg = self._message_cache[cache_key]
                if now - cached_time < Config.CACHE_TTL:
                    results_map[m_id] = cached_msg
                    continue
            missing_ids.append(m_id)
            
        if missing_ids:
            try:
                fetched = await self.client.get_messages(chat_id=target_chat, message_ids=missing_ids)
                if not isinstance(fetched, list):
                    fetched = [fetched] if fetched else []
                for msg in fetched:
                    if msg and getattr(msg, "id", None):
                        self._message_cache[f"{target_chat}:{msg.id}"] = (now, msg)
                        try:
                            self._message_cache[f"{int(target_chat)}:{int(msg.id)}"] = (now, msg)
                        except Exception:
                            pass
                        results_map[msg.id] = msg
            except Exception as e:
                logger.warning(f"Batch fetch failed for messages {missing_ids} in {target_chat}: {e}")
                for m_id in missing_ids:
                    try:
                        msg = await self.get_message(m_id, chat_id=target_chat)
                        if msg:
                            results_map[m_id] = msg
                    except Exception:
                        pass
                        
        return [results_map[m_id] for m_id in message_ids if m_id in results_map]

    async def get_message(self, message_id: int, chat_id: int = None) -> Message:
        if not self.is_running:
            await self.start()
            
        if chat_id is not None:
            target_chat = chat_id
        else:
            channel_ids = self.get_channel_ids()
            if channel_ids:
                target_chat = channel_ids[0]
            else:
                raise ValueError("chat_id is required when TELEGRAM_CHANNEL_ID is not configured")
        
        cache_key = f"{target_chat}:{message_id}"
        now = time.time()
        if cache_key in self._message_cache:
            cached_time, cached_msg = self._message_cache[cache_key]
            if now - cached_time < Config.CACHE_TTL:
                return cached_msg

        try:
            msg = await self.client.get_messages(chat_id=target_chat, message_ids=message_id)
            if msg:
                self._message_cache[cache_key] = (now, msg)
                return msg
        except Exception as e:
            err_name = type(e).__name__
            if "PeerIdInvalid" in err_name or "PEER_ID_INVALID" in str(e).upper():
                try:
                    logger.info(f"PeerIdInvalid for {target_chat}, refreshing dialogs...")
                    async for _ in self.client.get_dialogs(limit=300):
                        pass
                    msg = await self.client.get_messages(chat_id=target_chat, message_ids=message_id)
                    if msg:
                        self._message_cache[cache_key] = (now, msg)
                        return msg
                except Exception as re_err:
                    logger.error(f"Failed to resolve dialogs after PeerIdInvalid: {re_err}")
            
            # Check if message is in search cache
            for k, (ctime, cached_m) in self._message_cache.items():
                if getattr(cached_m, "id", None) == message_id:
                    logger.info(f"Using search-cached message for {target_chat}:{message_id}")
                    return cached_m
                    
            logger.error(f"Failed to fetch message {message_id} in channel {target_chat}: {e}")
            raise e

    def _has_media(self, msg: Message) -> bool:
        return bool(msg.video or msg.document or msg.audio)

tg_client_manager = TelegramClientManager()
