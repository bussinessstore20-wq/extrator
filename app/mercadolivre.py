import asyncio
from datetime import datetime, timezone, timedelta
import httpx
import logging
from app import config
from app.db import get_db, log_event

API = "https://api.mercadolibre.com"
logger = logging.getLogger(__name__)

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
        if response.status_code >= 400:
            # Ausência de ranking é esperada para várias categorias; não é falha de autenticação.
            body = response.text[:800]
            if response.status_code == 404 and "/highlights/" in path:
                logger.debug("Ranking indisponível: %s (HTTP 404)", path)
                raise RuntimeError(f"Mercado Livre API HTTP 404 em {path}: {body[:400]}")
            logger.error(
                "Mercado Livre API rejeitou %s: HTTP %s; resposta=%s",
                path, response.status_code, body
            )
            if response.status_code == 403:
                raise RuntimeError(
                    f"Mercado Livre negou acesso ao recurso {path} (HTTP 403). "
                    f"Detalhe da API: {body[:400]}. Verifique a autorização OAuth, "
                    "o status/permissões do aplicativo e possíveis restrições de acesso."
                )
            raise RuntimeError(f"Mercado Livre API HTTP {response.status_code} em {path}: {body[:400]}")
        return response.json()

async def get_categories() -> list[dict]:
    # O endpoint do site retorna categorias-raiz; destaques geralmente exige folhas.
    # Cache de 24h no Supabase evita reconstruir a árvore em cada ciclo.
    db = get_db()
    try:
        rows = db.table("extrator_settings").select("value").eq("key", "ml_leaf_categories").limit(1).execute().data or []
        cached = rows[0].get("value") if rows else None
        if isinstance(cached, dict):
            saved_at = datetime.fromisoformat(str(cached.get("saved_at", "")).replace("Z", "+00:00"))
            cats = cached.get("categories")
            if saved_at > datetime.now(timezone.utc) - timedelta(hours=24) and isinstance(cats, list) and cats:
                return cats
    except Exception:
        logger.info("Cache de categorias-folha ausente; reconstruindo.")

    roots = await api_get(f"/sites/{config.ML_SITE_ID}/categories")
    queue = list(roots or [])
    leaves: list[dict] = []
    visited: set[str] = set()
    max_nodes = 180
    while queue and len(visited) < max_nodes:
        category = queue.pop(0)
        category_id = str(category.get("id") or "").strip()
        if not category_id or category_id in visited:
            continue
        visited.add(category_id)
        try:
            details = await api_get(f"/categories/{category_id}")
        except Exception as exc:
            logger.debug("Categoria %s não pôde ser expandida: %s", category_id, str(exc)[:120])
            continue
        children = details.get("children_categories") or []
        if children:
            queue.extend(child for child in children if isinstance(child, dict))
        else:
            leaves.append({"id": category_id, "name": details.get("name") or category.get("name") or category_id})
    if not leaves:
        return roots or []
    value = {"saved_at": datetime.now(timezone.utc).isoformat(), "categories": leaves}
    try:
        db.table("extrator_settings").upsert({"key": "ml_leaf_categories", "value": value}).execute()
    except Exception:
        logger.exception("Falha ao salvar cache de categorias-folha.")
    logger.info("Categorias-folha descobertas: %s (nós consultados: %s).", len(leaves), len(visited))
    return leaves

def normalize_item(item: dict) -> dict | None:
    """Converte um anúncio real da API para o formato interno do Extrator."""
    item_id = str(item.get("id") or "").strip()
    title = str(item.get("title") or "").strip()
    permalink = str(item.get("permalink") or "").strip()
    if not item_id or not title or not permalink:
        return None
    pictures = item.get("pictures") or []
    thumbnail = item.get("thumbnail")
    if not thumbnail and pictures and isinstance(pictures[0], dict):
        thumbnail = pictures[0].get("secure_url") or pictures[0].get("url")
    return {
        "id": item_id,
        "title": title,
        "price": item.get("price"),
        "currency_id": item.get("currency_id") or "BRL",
        "thumbnail": thumbnail,
        "permalink": permalink,
        "condition": item.get("condition"),
        "available_quantity": item.get("available_quantity"),
        "official_store_id": item.get("official_store_id"),
    }


async def get_user_product_items(user_product_id: str) -> list[str]:
    """Resolve um User Product para os IDs de anúncios associados."""
    up = await api_get(f"/user-products/{user_product_id}")
    seller_id = up.get("user_id")
    if not seller_id:
        return []
    result = await api_get(
        f"/users/{seller_id}/items/search",
        params={"user_product_id": user_product_id, "limit": 10},
    )
    return [str(x) for x in (result.get("results") or []) if x]


async def search_category(category_id: str) -> list[dict]:
    # O endpoint de destaques só tem ranking para algumas categorias-folha.
    # Um 404 aqui significa normalmente "sem ranking disponível", não falha fatal.
    limit = max(1, min(config.COLLECTOR_MAX_ITEMS_PER_CATEGORY, 20))
    try:
        data = await api_get(f"/highlights/{config.ML_SITE_ID}/category/{category_id}")
    except RuntimeError as exc:
        if "HTTP 404" in str(exc):
            logger.info("Categoria %s sem ranking de mais vendidos; ignorada.", category_id)
            return []
        raise

    content = data.get("content", []) if isinstance(data, dict) else []
    items: list[dict] = []
    seen_ids: set[str] = set()

    for entry in content[:limit]:
        if not isinstance(entry, dict):
            continue
        entity_id = str(entry.get("id") or "").strip()
        entity_type = str(entry.get("type") or "").upper()
        if not entity_id:
            continue

        candidate_ids: list[str] = []
        if entity_type == "PRODUCT":
            # Produtos de catálogo nem sempre têm publicação vencedora.
            try:
                product = await api_get(f"/products/{entity_id}")
            except Exception as exc:
                logger.info("Catálogo %s indisponível: %s", entity_id, str(exc)[:140])
                continue
            winner = product.get("buy_box_winner") if isinstance(product, dict) else None
            if isinstance(winner, dict) and winner.get("item_id"):
                candidate_ids.append(str(winner["item_id"]))
            else:
                # Produtos de catálogo sem buy_box_winner não são compráveis;
                # não os gravamos como ofertas.
                logger.debug("Produto de catálogo %s sem anúncio vencedor.", entity_id)
                continue
        elif entity_type == "USER_PRODUCT" or entity_id.startswith("MLBU"):
            logger.debug(
                "User Product %s ignorado: acesso não disponível para a aplicação.",
                entity_id
            )
            continue
        elif entity_type == "ITEM":
            candidate_ids.append(entity_id)
        else:
            logger.debug("Tipo de destaque não suportado: %s (%s).", entity_type, entity_id)
            continue

        for item_id in candidate_ids:
            if item_id in seen_ids:
                continue
            seen_ids.add(item_id)
            try:
                raw_item = await api_get(f"/items/{item_id}")
            except Exception as exc:
                logger.info("Anúncio %s não acessível pela API: %s", item_id, str(exc)[:160])
                continue
            if isinstance(raw_item, dict):
                normalized = normalize_item(raw_item)
                if normalized:
                    items.append(normalized)

    return items

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
    # Diagnóstico seguro: valida o token armazenado e registra apenas o ID da conta,
    # nunca o access token ou refresh token. Ajuda a separar falha OAuth de bloqueio de catálogo.
    account = await api_get("/users/me")
    logger.info("Token OAuth Mercado Livre aceito em /users/me; user_id=%s", account.get("id"))
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
