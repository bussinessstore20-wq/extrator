import asyncio
from datetime import datetime, timezone, timedelta
import httpx
from app import config
from app.db import get_db, log_event

API = "https://api.mercadolibre.com"

def get_saved_tokens() -> dict:
    try:
        rows = get_db().table("extrator_settings").select("value").eq("key", "ml_oauth").limit(1).execute().data
        if rows:
            return rows[0].get("value") or {}
    except Exception:
        pass
    return {}

async def access_token() -> str:
    saved = get_saved_tokens()
    token = saved.get("access_token") or config.ML_ACCESS_TOKEN
    refresh = saved.get("refresh_token") or config.ML_REFRESH_TOKEN
    expires_at = saved.get("expires_at")
    if token and expires_at:
        try:
            expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            if expiry > datetime.now(timezone.utc) + timedelta(seconds=60):
                return token
        except (TypeError, ValueError):
            pass
    elif token and not refresh:
        return token
    if not refresh or not config.ML_CLIENT_ID or not config.ML_CLIENT_SECRET:
        return token or ""
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(f"{API}/oauth/token", data={
            "grant_type": "refresh_token",
            "client_id": config.ML_CLIENT_ID,
            "client_secret": config.ML_CLIENT_SECRET,
            "refresh_token": refresh,
        })
        response.raise_for_status()
        data = response.json()
    expires_in = int(data.get("expires_in") or 0)
    saved = {
        "access_token": data.get("access_token", ""),
        "refresh_token": data.get("refresh_token", refresh),
        "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=max(0, expires_in))).isoformat(),
    }
    get_db().table("extrator_settings").upsert({"key": "ml_oauth", "value": saved}).execute()
    return saved["access_token"]

async def api_get(path: str, params: dict | None = None):
    token = await access_token()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with httpx.AsyncClient(timeout=25, headers=headers) as client:
        response = await client.get(f"{API}{path}", params=params or {})
        if response.status_code == 429:
            raise RuntimeError("Mercado Livre limitou as requisições (HTTP 429); a coleta será retomada no próximo ciclo.")
        response.raise_for_status()
        return response.json()

async def get_categories() -> list[dict]:
    return await api_get(f"/sites/{config.ML_SITE_ID}/categories")

async def search_category(category_id: str) -> list[dict]:
    data = await api_get(f"/sites/{config.ML_SITE_ID}/search", {
        "category": category_id,
        "limit": config.COLLECTOR_MAX_ITEMS_PER_CATEGORY,
        "sort": "relevance",
    })
    return data.get("results", [])

def persist_product(item: dict, category_id: str) -> bool:
    item_id = str(item.get("id") or "").strip()
    permalink = str(item.get("permalink") or "").strip()
    title = str(item.get("title") or "").strip()
    if not item_id or not permalink or not title:
        return False
    db = get_db()
    existing = db.table("extrator_products").select("id,status").eq("site_id", config.ML_SITE_ID).eq("item_id", item_id).limit(1).execute().data
    values = {
        "site_id": config.ML_SITE_ID,
        "item_id": item_id,
        "category_id": category_id,
        "title": title[:500],
        "price": item.get("price"),
        "currency_id": item.get("currency_id"),
        "thumbnail": item.get("thumbnail"),
        "permalink": permalink,
        "metadata": {
            "condition": item.get("condition"),
            "available_quantity": item.get("available_quantity"),
            "official_store_id": item.get("official_store_id"),
        },
        "last_checked_at": datetime.now(timezone.utc).isoformat(),
    }
    if existing:
        # Keep human decisions intact; refresh only product data.
        db.table("extrator_products").update({
            k: v for k, v in values.items() if k not in {"site_id", "item_id"}
        }).eq("id", existing[0]["id"]).execute()
        return False
    values["status"] = "pending_approval"
    inserted = db.table("extrator_products").insert(values).execute().data
    if inserted:
        log_event("product_discovered", inserted[0]["id"], details={"item_id": item_id, "category_id": category_id})
        return True
    return False

async def collect_once() -> dict:
    categories = await get_categories()
    if not isinstance(categories, list) or not categories:
        raise RuntimeError("A API não retornou categorias disponíveis para a coleta.")
    db = get_db()
    settings_rows = db.table("extrator_settings").select("value").eq("key", "collector").limit(1).execute().data
    settings = settings_rows[0].get("value", {}) if settings_rows else {}
    cursor = int(settings.get("category_cursor", 0)) % len(categories)
    count = min(config.COLLECTOR_CATEGORIES_PER_CYCLE, len(categories))
    selected = [categories[(cursor + i) % len(categories)] for i in range(count)]
    run = db.table("extrator_collection_runs").insert({"status": "running"}).execute().data[0]
    seen = new_items = errors = 0
    for category in selected:
        category_id = str(category.get("id", ""))
        if not category_id:
            continue
        try:
            items = await search_category(category_id)
            seen += len(items)
            for item in items:
                try:
                    new_items += int(persist_product(item, category_id))
                except Exception as exc:
                    errors += 1
                    log_event("product_persist_error", details={"item_id": item.get("id"), "error": str(exc)[:300]})
            await asyncio.sleep(1.0)
        except Exception as exc:
            errors += 1
            log_event("category_collection_error", details={"category_id": category_id, "error": str(exc)[:500]})
    next_cursor = (cursor + count) % len(categories)
    settings.update({"enabled": config.COLLECTOR_ENABLED, "category_cursor": next_cursor})
    db.table("extrator_settings").upsert({"key": "collector", "value": settings}).execute()
    db.table("extrator_collection_runs").update({
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "status": "completed" if errors == 0 else "partial",
        "categories_seen": count,
        "items_seen": seen,
        "new_items": new_items,
        "error_count": errors,
        "details": {"next_category_cursor": next_cursor},
    }).eq("id", run["id"]).execute()
    return {"categories_seen": count, "items_seen": seen, "new_items": new_items, "errors": errors}
