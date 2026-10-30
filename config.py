import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

class Config:
    PORT = int(os.getenv("PORT", 7860))
    ADDON_URL = os.getenv("ADDON_URL", f"http://localhost:{PORT}").rstrip("/")
    API_KEY = os.getenv("API_KEY", "")
    CACHE_TTL = int(os.getenv("CACHE_TTL", 1800))
    TIMEZONE = os.getenv("TIMEZONE", "UTC")

    API_ID = os.getenv("API_ID")
    API_HASH = os.getenv("API_HASH")
    BOT_TOKEN = os.getenv("BOT_TOKEN")
    USER_SESSION_STRING = os.getenv("USER_SESSION_STRING", "")

    TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")
    LOG_CHANNEL_ID = os.getenv("LOG_CHANNEL_ID")
    try:
        PARALLEL_CONNECTIONS = int(str(os.getenv("PARALLEL_CONNECTIONS", "4")).strip() or 4)
    except (ValueError, TypeError):
        PARALLEL_CONNECTIONS = 4

    # Enable Telegram Search catalog in Stremio (default: False)
    _raw_search = os.getenv("ENABLE_TELEGRAM_SEARCH") or os.getenv("ENABLE_SEARCH", "false")
    ENABLE_TELEGRAM_SEARCH = str(_raw_search).strip().lower() in ("true", "1", "yes", "on")
    ENABLE_SEARCH = ENABLE_TELEGRAM_SEARCH

    # Hide split video files when loading stream links for movies and series (default: False)
    _raw_hide_split = (
        os.getenv("HIDE_SPLIT_STREAMS")
        or os.getenv("HIDE_SPLIT_FILES")
        or os.getenv("HIDE_SPLIT")
        or os.getenv("HIDE_SPLIT_IN_STREAMS", "false")
    )
    HIDE_SPLIT_STREAMS = str(_raw_hide_split).strip().lower() in ("true", "1", "yes", "on")
    HIDE_SPLIT_FILES = HIDE_SPLIT_STREAMS

    @classmethod
    def validate(cls):
        missing = []
        if not cls.API_ID:
            missing.append("API_ID")
        if not cls.API_HASH:
            missing.append("API_HASH")
        if not cls.BOT_TOKEN and not cls.USER_SESSION_STRING:
            missing.append("BOT_TOKEN or USER_SESSION_STRING")
        if missing:
            raise ValueError(
                f"Missing critical configuration variables: {', '.join(missing)}. "
                "Please configure them in your environment or a .env file."
            )

        try:
            cls.API_ID = int(cls.API_ID)
        except (ValueError, TypeError):
            raise ValueError("API_ID must be a valid integer.")

        if cls.TELEGRAM_CHANNEL_ID and isinstance(cls.TELEGRAM_CHANNEL_ID, str):
            val = cls.TELEGRAM_CHANNEL_ID.strip()
            if val.startswith("-") or val.isdigit():
                try:
                    cls.TELEGRAM_CHANNEL_ID = int(val)
                except ValueError:
                    pass

        if cls.LOG_CHANNEL_ID and isinstance(cls.LOG_CHANNEL_ID, str):
            val = cls.LOG_CHANNEL_ID.strip()
            if val.startswith("-") or val.isdigit():
                try:
                    cls.LOG_CHANNEL_ID = int(val)
                except ValueError:
                    pass
