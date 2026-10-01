from functools import lru_cache
from supabase import create_client, Client
from app import config

@lru_cache(maxsize=1)
def get_db() -> Client:
    if not config.supabase_ready():
        raise RuntimeError("Supabase não configurado: defina SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY no Render.")
    return create_client(config.SUPABASE_URL, config.SUPABASE_SERVICE_ROLE_KEY)

def log_event(event_type: str, product_id: str | None = None, actor_id: int | None = None, details: dict | None = None) -> None:
    get_db().table("extrator_audit_logs").insert({
        "event_type": event_type,
        "product_id": product_id,
        "actor_telegram_id": actor_id,
        "details": details or {},
    }).execute()
