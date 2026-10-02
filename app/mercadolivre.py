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
    """Retorna termos de busca públicos para coleta de links."""
    return [
        {"id": "ofertas", "name": "Ofertas"},
        {"id": "eletronicos", "name": "Eletrônicos"},
        {"id": "casa", "name": "Casa"},
        {"id": "beleza", "name": "Beleza"},
        {"id": "moda", "name": "Moda"},
    ]


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



class _ListingParser(HTMLParser):
    """Extrai URLs de anúncios MLB de páginas públicas."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
    def handle_starttag(self, tag, attrs):
        if tag.lower() == "a":
            href = (dict(attrs).get("href") or "").strip()
            if href:
                self.links.append(href)


async def public_category_fallback(category_id: str, limit: int) -> list[dict]:
    """Retorna somente links e IDs, sem consultar os detalhes individuais."""
    import re
    from urllib.parse import quote, urljoin
    query = str(category_id or "ofertas").strip().replace("_", " ")
    url = "https://lista.mercadolivre.com.br/" + quote(query.replace(" ", "-"))
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "pt-BR,pt;q=0.9",
    }
    found, seen = [], set()
    try:
        async with httpx.AsyncClient(timeout=25, follow_redirects=True, headers=headers) as client:
            response = await client.get(url)
            logger.info("Listagem pública '%s': HTTP %s (%s bytes)", query, response.status_code, len(response.text))
            if response.status_code >= 400:
                return []
            parser = _ListingParser()
            parser.feed(response.text[:5000000])
            for href in parser.links:
                link = urljoin(str(response.url), href).split("#", 1)[0]
                match = re.search(r"/MLB-?([0-9]{6,})(?=[/?]|$)", link, re.I)
                if not match:
                    continue
                item_id = "MLB" + match.group(1)
                if item_id in seen or "/p/" in link.lower():
                    continue
                seen.add(item_id)
                found.append({
                    "id": item_id, "title": item_id, "price": None,
                    "currency_id": "BRL", "thumbnail": None, "permalink": link,
                    "condition": None, "available_quantity": None, "official_store_id": None,
                })
                if len(found) >= limit:
                    break
    except Exception as exc:
        logger.warning("Coleta de links públicos '%s' falhou: %s", query, str(exc)[:160])
    logger.info("Links públicos encontrados para '%s': %s", query, len(found))
    return found[:limit]


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
    limit = max(1, min(config.COLLECTOR_MAX_ITEMS_PER_CATEGORY, 20))
    return await public_category_fallback(category_id, limit)


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
