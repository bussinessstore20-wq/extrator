import os

def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}

def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_ADMIN_IDS = {
    int(x.strip()) for x in os.getenv("TELEGRAM_ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID", "").strip()
ML_SITE_ID = os.getenv("ML_SITE_ID", "MLB").strip().upper()
ML_CLIENT_ID = os.getenv("ML_CLIENT_ID", "").strip()
ML_CLIENT_SECRET = os.getenv("ML_CLIENT_SECRET", "").strip()
ML_ACCESS_TOKEN = os.getenv("ML_ACCESS_TOKEN", "").strip()
ML_REFRESH_TOKEN = os.getenv("ML_REFRESH_TOKEN", "").strip()
ML_REDIRECT_URI = os.getenv("ML_REDIRECT_URI", "").strip()
ML_OAUTH_STATE = os.getenv("ML_OAUTH_STATE", "").strip()
COLLECTOR_ENABLED = env_bool("COLLECTOR_ENABLED", False)
COLLECTOR_INTERVAL_SECONDS = max(60, env_int("COLLECTOR_INTERVAL_SECONDS", 900))
COLLECTOR_CATEGORIES_PER_CYCLE = max(1, env_int("COLLECTOR_CATEGORIES_PER_CYCLE", 5))
COLLECTOR_MAX_ITEMS_PER_CATEGORY = min(50, max(1, env_int("COLLECTOR_MAX_ITEMS_PER_CATEGORY", 50)))
ADMIN_API_SECRET = os.getenv("ADMIN_API_SECRET", "").strip()

def supabase_ready() -> bool:
    return bool(SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY)

def telegram_ready() -> bool:
    return bool(TELEGRAM_BOT_TOKEN and TELEGRAM_ADMIN_IDS and TELEGRAM_CHANNEL_ID)

def mercadolivre_ready() -> bool:
    return bool(ML_CLIENT_ID and ML_CLIENT_SECRET and ML_REDIRECT_URI)

TELEGRAM_WEBHOOK_URL = os.getenv(
    "TELEGRAM_WEBHOOK_URL",
    "https://extrator-aclt.onrender.com/telegram/webhook"
).strip()
