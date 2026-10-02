import asyncio
from datetime import datetime, timezone, timedelta
import httpx
import logging
from html.parser import HTMLParser
from html import unescape
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

class _MetaParser(HTMLParser):
    """Extrai metadados públicos de uma página de anúncio, sem executar JavaScript."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta = {}
        self.canonical = ""

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag.lower() == "meta":
            key = (attrs.get("property") or attrs.get("name") or "").strip().lower()
            value = (attrs.get("content") or "").strip()
            if key and value:
                self.meta[key] = value
        elif tag.lower() == "link" and "canonical" in (attrs.get("rel") or "").lower():
            self.canonical = (attrs.get("href") or "").strip()


async def public_page_fallback(item_id: str) -> dict | None:
    """
    Fallback para anúncios públicos quando a API /items/{id} devolve 403.
    Usa apenas metadados da página pública; não inventa preço ou título ausentes.
    """
    if not item_id.startswith("MLB") or not item_id[3:].isdigit():
        return None
    page_url = f"https://produto.mercadolivre.com.br/MLB-{item_id[3:]}"
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; Extrator/1.0)",
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "pt-BR,pt;q=0.9",
    }
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers=headers) as client:
            response = await client.get(page_url)
        if response.status_code >= 400:
            logger.info("Fallback público do anúncio %s respondeu HTTP %s.", item_id, response.status_code)
            return None
        parser = _MetaParser()
        parser.feed(response.text[:2_000_000])
        meta = parser.meta
        title = unescape(meta.get("og:title") or meta.get("twitter:title") or "").strip()
        title = title.replace(" | Mercado Livre", "").replace(" - Mercado Livre", "").strip()
        permalink = parser.canonical or str(response.url)
        thumbnail = meta.get("og:image") or meta.get("twitter:image")
        price_raw = meta.get("product:price:amount") or meta.get("og:price:amount")
        currency = meta.get("product:price:currency") or meta.get("og:price:currency") or "BRL"
        try:
            price = float(price_raw.replace(",", ".")) if price_raw else None
        except (TypeError, ValueError):
            price = None
        if not title or not permalink:
            logger.info("Fallback público do anúncio %s não encontrou título/link nos metadados.", item_id)
            return None
        logger.info("Anúncio %s recuperado por metadados da página pública.", item_id)
        return {
            "id": item_id,
            "title": title,
            "price": price,
            "currency_id": currency,
            "thumbnail": thumbnail,
            "permalink": permalink,
            "condition": None,
            "available_quantity": None,
            "official_store_id": None,
        }
    except Exception as exc:
        logger.info("Fallback público do anúncio %s falhou: %s", item_id, str(exc)[:180])
        return None



async def public_category_fallback(category_id: str, limit: int) -> list[dict]:
    """Busca IDs de anúncios na página pública da categoria como último recurso."""
    import re

    url = "https://lista.mercadolivre.com.br/_Desde_1_NoIndex_True"
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
    }
    try:
        async with httpx.AsyncClient(timeout=25, follow_redirects=True, headers=headers) as client:
            response = await client.get(url, params={"category": category_id})
        if response.status_code >= 400:
            logger.warning("Busca pública da categoria %s respondeu HTTP %s.", category_id, response.status_code)
            return []
        html = response.text[:5_000_000]
        ids: list[str] = []
        for pattern in (r'(?<![A-Z0-9])MLB-([0-9]{6,})(?![0-9])', r'(?<![A-Z0-9])MLB([0-9]{6,})(?![0-9])'):
            for match in re.finditer(pattern, html):
                item_id = "MLB" + match.group(1)
                if item_id not in ids:
                    ids.append(item_id)
                if len(ids) >= limit * 3:
                    break
            if len(ids) >= limit * 3:
                break
        recovered = []
        for item_id in ids:
            item = await public_page_fallback(item_id)
            if item:
                recovered.append(item)
            if len(recovered) >= limit:
                break
        logger.info("Fallback público da categoria %s: IDs encontrados=%s, produtos recuperados=%s", category_id, len(ids), len(recovered))
        return recovered
    except Exception as exc:
        logger.warning("Fallback de página pública da categoria %s falhou: %s", category_id, str(exc)[:180])
        return []

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
    """
    Procura ofertas pelo ranking de destaques e usa a busca oficial do site
    como alternativa quando a categoria não possui ranking ou não gera ofertas.
    """
    limit = max(1, min(config.COLLECTOR_MAX_ITEMS_PER_CATEGORY, 20))
    items: list[dict] = []
    seen_ids: set[str] = set()
    content: list[dict] = []
    candidate_ids: list[str] = []

    try:
        data = await api_get(f"/highlights/{config.ML_SITE_ID}/category/{category_id}")
        content = data.get("content", []) if isinstance(data, dict) else []
    except RuntimeError as exc:
        if "HTTP 404" not in str(exc):
            logger.warning("Destaques da categoria %s falharam: %s", category_id, str(exc)[:180])
        else:
            logger.info("Categoria %s sem ranking; usando busca oficial como alternativa.", category_id)

    for entry in content[:limit]:
        if not isinstance(entry, dict):
            continue
        entity_id = str(entry.get("id") or "").strip()
        entity_type = str(entry.get("type") or "").upper()
        if not entity_id:
            continue
        if entity_type == "ITEM" and entity_id.startswith("MLB"):
            candidate_ids.append(entity_id)
        elif entity_type == "PRODUCT":
            try:
                product = await api_get(f"/products/{entity_id}")
                winner = product.get("buy_box_winner") if isinstance(product, dict) else None
                if isinstance(winner, dict) and winner.get("item_id"):
                    candidate_ids.append(str(winner["item_id"]))
            except Exception as exc:
                logger.info("Catálogo %s indisponível: %s", entity_id, str(exc)[:120])
        elif entity_type == "USER_PRODUCT" or entity_id.startswith("MLBU"):
            try:
                candidate_ids.extend(await get_user_product_items(entity_id))
            except Exception as exc:
                logger.info("User Product %s não resolvido: %s", entity_id, str(exc)[:120])
        else:
            logger.debug("Tipo de destaque não suportado: %s (%s).", entity_type, entity_id)

    async def fetch_candidates(ids: list[str]) -> None:
        for item_id in ids:
            item_id = str(item_id or "").strip()
            if not item_id.startswith("MLB") or item_id in seen_ids:
                continue
            seen_ids.add(item_id)
            normalized = None
            try:
                raw_item = await api_get(f"/items/{item_id}")
                if isinstance(raw_item, dict):
                    normalized = normalize_item(raw_item)
            except Exception as exc:
                message = str(exc)
                if "HTTP 403" in message or "access_denied" in message:
                    normalized = await public_page_fallback(item_id)
                if normalized is None:
                    logger.info("Anúncio %s não recuperado por API/fallback: %s", item_id, message[:160])
            if normalized:
                items.append(normalized)

    await fetch_candidates(candidate_ids[:limit])

    # A API de destaques não possui ranking para todas as categorias. A busca
    # oficial /sites/{site}/search é o caminho de fallback documentado pelo ML.
    if len(items) < limit:
        try:
            data = await api_get(
                f"/sites/{config.ML_SITE_ID}/search",
                params={"category": category_id, "limit": limit, "sort": "relevance"},
            )
            search_results = data.get("results", []) if isinstance(data, dict) else []
            search_ids = [
                str(row.get("id") or "")
                for row in search_results
                if isinstance(row, dict) and row.get("id")
            ]
            before = len(items)
            await fetch_candidates(search_ids)
            logger.info(
                "Fallback de busca da categoria %s: resultados_api=%s, novos_validos=%s",
                category_id, len(search_results), len(items) - before
            )
        except Exception as exc:
            logger.warning("Busca oficial da categoria %s falhou: %s", category_id, str(exc)[:200])
            public_items = await public_category_fallback(category_id, limit - len(items))
            for public_item in public_items:
                if public_item["id"] not in seen_ids:
                    seen_ids.add(public_item["id"])
                    items.append(public_item)
    if len(items) < limit and not content and not items:
        public_items = await public_category_fallback(category_id, limit - len(items))
        for public_item in public_items:
            if public_item["id"] not in seen_ids:
                seen_ids.add(public_item["id"])
                items.append(public_item)

    logger.info(
        "Categoria %s: destaques=%s, IDs candidatos=%s, produtos válidos=%s",
        category_id, len(content), len(candidate_ids), len(items)
    )
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
