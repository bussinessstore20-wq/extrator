"""Shopee Affiliate Open API collector with public product-page origin verification.

Automatic collection is opt-in and disabled by default. Product detail pages are
read only to inspect the visible "Enviado de" location; no login or access-control
bypass is attempted.
"""
import asyncio
import hashlib
import html
import json
import logging
import re
import time
from urllib.parse import urlparse
from datetime import datetime, timezone

import httpx

from app import config
from app.db import get_db, log_event

API_URL = "https://open-api.affiliate.shopee.com.br/graphql"
logger = logging.getLogger(__name__)


BRAZILIAN_STATES = (
    "acre", "alagoas", "amapa", "amazonas", "bahia", "ceara", "distrito federal",
    "espirito santo", "goias", "maranhao", "mato grosso", "mato grosso do sul",
    "minas gerais", "para", "paraiba", "parana", "pernambuco", "piaui",
    "rio de janeiro", "rio grande do norte", "rio grande do sul", "rondonia",
    "roraima", "santa catarina", "sao paulo", "sergipe", "tocantins",
)
BRAZILIAN_CITIES = (
    "sao paulo", "rio de janeiro", "brasilia", "belo horizonte", "vitoria",
    "goiania", "cuiaba", "campo grande", "curitiba", "florianopolis", "porto alegre",
    "salvador", "recife", "fortaleza", "natal", "joao pessoa", "maceio", "aracaju",
    "teresina", "sao luis", "belem", "manaus", "macapa", "boa vista", "palmas",
    "rio branco", "porto velho", "campinas", "guarulhos", "santo andre", "santos",
    "sorocaba", "ribeirao preto", "uberlandia", "juiz de fora", "londrina", "maringa",
    "joinville", "blumenau", "caxias do sul", "pelotas", "contagem", "betim",
    "feira de santana", "caruaru", "petrolina", "jaboatao dos guararapes",
)
FOREIGN_ORIGINS = (
    "china", "coreia", "coréia", "japao", "japão", "hong kong", "singapura",
    "estados unidos", "united states", "vietna", "vietnã", "tailandia", "tailândia",
    "malasia", "malásia", "indonesia", "indonésia", "internacional", "importado do",
)


def _normalise_origin(value: str) -> str:
    import unicodedata
    value = unicodedata.normalize("NFKD", value or "")
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", value).strip().lower()


def extract_shipping_origin(page_html: str) -> str | None:
    """Read the public product-page 'Enviado de' label; no guessed origin."""
    decoded = html.unescape(page_html or "")
    decoded = re.sub(r"<[^>]+>", " ", decoded)
    decoded = decoded.replace("\\u00e3", "ã").replace("\\u00ed", "í")
    decoded = re.sub(r"\s+", " ", decoded)
    match = re.search(
        r"(?:Enviado\s+de|Shipping\s+from)\s*:?\s*"
        r"([A-Za-zÀ-ÿ][A-Za-zÀ-ÿ .,'-]{1,55})",
        decoded,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    origin = match.group(1)
    origin = re.split(
        r"\b(?:descri[cç][aã]o|detalhes do produto|frete|estoque|categoria|"
        r"adicionar ao carrinho|comprar agora|garantia shopee)\b",
        origin,
        flags=re.IGNORECASE,
    )[0]
    origin = re.sub(r"^[\s:,-]+|[\s:,-]+$", "", origin)
    return origin[:60] or None


def classify_shipping_origin(product: dict) -> tuple[bool, str]:
    """Only classify a product when its public detail page exposes a Brazilian origin."""
    metadata = product.get("metadata") or {}
    origin = str(metadata.get("shipping_origin_page") or "").strip()
    normalized = _normalise_origin(origin)
    if not normalized:
        return False, "origin_unknown"
    foreign = {_normalise_origin(value) for value in FOREIGN_ORIGINS}
    if any(value in normalized for value in foreign):
        return False, "international"
    states = {_normalise_origin(value) for value in BRAZILIAN_STATES}
    cities = {_normalise_origin(value) for value in BRAZILIAN_CITIES}
    if any(re.search(r"\b" + re.escape(state) + r"\b", normalized) for state in states):
        return True, "national_confirmed"
    if normalized in cities:
        return True, "national_confirmed"
    return False, "origin_unknown"


async def fetch_shipping_origin(item: dict, semaphore: asyncio.Semaphore) -> str | None:
    """Check Shopee's public product detail page for its displayed dispatch location."""
    metadata = item.get("metadata") or {}
    product_url = str(metadata.get("product_link") or "").strip()
    parsed = urlparse(product_url)
    if parsed.scheme != "https" or parsed.hostname not in {"shopee.com.br", "www.shopee.com.br"}:
        return None
    async with semaphore:
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(4.0, connect=2.0),
                follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0 (compatible; Extrator/1.0; +https://shopee.com.br)"},
            ) as client:
                response = await client.get(product_url)
            final_host = urlparse(str(response.url)).hostname
            if response.status_code >= 400 or final_host not in {"shopee.com.br", "www.shopee.com.br"}:
                return None
            return extract_shipping_origin(response.text)
        except (httpx.HTTPError, ValueError) as exc:
            logger.info("Não foi possível confirmar origem no anúncio Shopee: %s", type(exc).__name__)
            return None
CATEGORIES = [
    {"id": "shopee_ofertas", "name": "Ofertas", "keyword": "ofertas"},
    {"id": "shopee_eletronicos", "name": "Eletrônicos", "keyword": "eletronicos"},
    {"id": "shopee_casa", "name": "Casa", "keyword": "casa"},
    {"id": "shopee_beleza", "name": "Beleza", "keyword": "beleza"},
    {"id": "shopee_moda", "name": "Moda", "keyword": "moda"},
]


def shopee_ready() -> bool:
    return bool(config.SHOPEE_AFFILIATE_APP_ID and config.SHOPEE_AFFILIATE_SECRET)


def _signed_headers(payload: str) -> dict[str, str]:
    if not shopee_ready():
        raise RuntimeError("API de afiliados Shopee não configurada. Defina SHOPEE_AFFILIATE_APP_ID e SHOPEE_AFFILIATE_SECRET no Render.")
    timestamp = str(int(time.time()))
    signature = hashlib.sha256(
        (config.SHOPEE_AFFILIATE_APP_ID + timestamp + payload + config.SHOPEE_AFFILIATE_SECRET).encode("utf-8")
    ).hexdigest()
    return {
        "Authorization": f"SHA256 Credential={config.SHOPEE_AFFILIATE_APP_ID}, Timestamp={timestamp}, Signature={signature}",
        "Content-Type": "application/json",
    }


async def graphql(query: str) -> dict:
    # Sign the exact JSON string that is sent in the HTTP body.
    payload = json.dumps({"query": query}, ensure_ascii=False, separators=(",", ":"))
    headers = _signed_headers(payload)
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(API_URL, content=payload.encode("utf-8"), headers=headers)
    if response.status_code >= 400:
        detail = response.text[:350].replace(config.SHOPEE_AFFILIATE_SECRET, "[redacted]")
        raise RuntimeError(f"API de afiliados Shopee respondeu HTTP {response.status_code}: {detail}")
    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError("A API de afiliados Shopee retornou uma resposta que não é JSON válido.") from exc
    if data.get("errors"):
        messages = []
        for error in data["errors"][:3]:
            extensions = error.get("extensions") or {}
            messages.append(str(extensions.get("message") or error.get("message") or "Erro GraphQL")[:180])
        raise RuntimeError("Erro GraphQL Shopee: " + "; ".join(messages))
    return data.get("data") or {}


def _gql_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


async def search_category(category: dict) -> list[dict]:
    keyword = _gql_string(str(category.get("keyword") or category.get("name") or "ofertas"))
    limit = min(50, max(1, config.SHOPEE_MAX_ITEMS_PER_CATEGORY))
    query = (
        "query { productOfferV2("
        f"keyword: {keyword}, listType: 0, sortType: 1, page: 1, limit: {limit}"
        ") { nodes { itemId productName productLink offerLink imageUrl "
        "priceMin priceMax priceDiscountRate sales ratingStar commissionRate "
        "sellerCommissionRate shopeeCommissionRate shopId shopName shopType } "
        "pageInfo { page limit hasNextPage } } }"
    )
    payload = await graphql(query)
    result = payload.get("productOfferV2") or {}
    nodes = result.get("nodes") or []
    items = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        item_id = str(node.get("itemId") or "").strip()
        shop_id = str(node.get("shopId") or "").strip()
        title = str(node.get("productName") or "").strip()
        product_link = str(node.get("productLink") or "").strip()
        offer_link = str(node.get("offerLink") or "").strip()
        image_url = str(node.get("imageUrl") or "").strip() or None
        if not item_id or not title or not (offer_link or product_link):
            continue
        price = node.get("priceMin")
        try:
            price = float(price) if price not in (None, "") else None
        except (TypeError, ValueError):
            price = None
        items.append({
            "id": f"{shop_id}-{item_id}" if shop_id else item_id,
            "title": title,
            "price": price,
            "currency_id": "BRL",
            "thumbnail": image_url,
            "permalink": offer_link or product_link,
            "metadata": {
                "platform": "shopee",
                "shop_id": shop_id or None,
                "item_id": item_id,
                "product_link": product_link or None,
                "affiliate_link": offer_link or None,
                "price_max": node.get("priceMax"),
                "discount_rate": node.get("priceDiscountRate"),
                "sales": node.get("sales"),
                "rating": node.get("ratingStar"),
                "commission_rate": node.get("commissionRate"),
                "seller_commission_rate": node.get("sellerCommissionRate"),
                "shopee_commission_rate": node.get("shopeeCommissionRate"),
                "shop_name": node.get("shopName"),
                "shop_type": node.get("shopType"),
                "shipping_icon_type": node.get("shippingIconType"),
                "cross_border_option": node.get("cbOption"),
            },
        })
    return items


def persist_product(item: dict, category_id: str) -> bool:
    item_id = str(item.get("id") or "").strip()
    permalink = str(item.get("permalink") or "").strip()
    title = str(item.get("title") or "").strip()
    if not item_id or not permalink or not title:
        return False
    db = get_db()
    existing = (
        db.table("extrator_products")
        .select("id,status")
        .eq("site_id", "SHOPEE")
        .eq("item_id", item_id)
        .limit(1)
        .execute()
        .data
    )
    values = {
        "site_id": "SHOPEE",
        "item_id": item_id,
        "category_id": category_id,
        "title": title[:500],
        "price": item.get("price"),
        "currency_id": item.get("currency_id") or "BRL",
        "thumbnail": item.get("thumbnail"),
        "permalink": permalink,
        "metadata": item.get("metadata") or {},
        "last_checked_at": datetime.now(timezone.utc).isoformat(),
    }
    if existing:
        # Refresh product fields without overwriting an existing approval decision.
        db.table("extrator_products").update({
            key: value for key, value in values.items() if key not in {"site_id", "item_id"}
        }).eq("id", existing[0]["id"]).execute()
        return False
    values["status"] = "pending_approval"
    inserted = db.table("extrator_products").insert(values).execute().data
    if inserted:
        log_event("product_discovered", inserted[0]["id"], details={
            "platform": "shopee", "item_id": item_id, "category_id": category_id,
        })
        return True
    return False


async def collect_once() -> dict:
    """Collect up to five new Shopee products, accepting only confirmed Brazilian origin."""
    if not shopee_ready():
        raise RuntimeError("Credenciais da API de afiliados Shopee ausentes.")
    if not config.supabase_ready():
        raise RuntimeError("Supabase não configurado.")
    categories = CATEGORIES
    db = get_db()
    settings_rows = db.table("extrator_settings").select("value").eq("key", "shopee_collector").limit(1).execute().data
    settings = (settings_rows[0].get("value") or {}) if settings_rows else {}
    cursor = int(settings.get("category_cursor", 0)) % len(categories)
    count = min(config.SHOPEE_CATEGORIES_PER_CYCLE, len(categories))
    selected = [categories[(cursor + i) % len(categories)] for i in range(count)]
    max_new_products = min(5, max(1, config.SHOPEE_MAX_ITEMS_PER_CYCLE))
    run = db.table("extrator_collection_runs").insert({
        "status": "running",
        "details": {
            "platform": "shopee",
            "max_new_products": max_new_products,
            "interval_seconds": config.SHOPEE_COLLECTOR_INTERVAL_SECONDS,
            "shipping_filter": "national_only_fail_closed",
        },
    }).execute().data[0]
    seen = new_items = errors = 0
    accepted_national = excluded_international = excluded_unknown_origin = 0
    category_errors = []
    origin_checks = 0
    origin_values_found = 0
    origin_check_limit = 50
    origin_semaphore = asyncio.Semaphore(10)
    for category in selected:
        if new_items >= max_new_products or origin_checks >= origin_check_limit:
            break
        try:
            items = await search_category(category)
            seen += len(items)
            candidates = items[: max(0, origin_check_limit - origin_checks)]
            origins = await asyncio.gather(
                *(fetch_shipping_origin(item, origin_semaphore) for item in candidates),
                return_exceptions=True,
            )
            origin_checks += len(candidates)
            for item, page_origin in zip(candidates, origins):
                if isinstance(page_origin, str) and page_origin:
                    origin_values_found += 1
                    item.setdefault("metadata", {})["shipping_origin_page"] = page_origin
                accepted, origin_status = classify_shipping_origin(item)
                item.setdefault("metadata", {})["shipping_origin_filter"] = origin_status
                if not accepted:
                    if origin_status == "international":
                        excluded_international += 1
                    else:
                        excluded_unknown_origin += 1
                    continue
                accepted_national += 1
                try:
                    new_items += int(persist_product(item, category["id"]))
                except Exception as exc:
                    errors += 1
                    log_event("product_persist_error", details={
                        "platform": "shopee", "item_id": item.get("id"), "error": str(exc)[:250],
                    })
            await asyncio.sleep(0.5)
        except Exception as exc:
            errors += 1
            error_text = str(exc)[:500]
            category_errors.append({"category_id": category["id"], "error": error_text})
            log_event("category_collection_error", details={
                "platform": "shopee", "category_id": category["id"], "error": error_text,
            })
    next_cursor = (cursor + count) % len(categories)
    settings.update({
        "enabled": config.SHOPEE_COLLECTOR_ENABLED,
        "category_cursor": next_cursor,
        "interval_seconds": config.SHOPEE_COLLECTOR_INTERVAL_SECONDS,
        "max_new_products_per_cycle": max_new_products,
        "shipping_origin_policy": "confirmed_national_only",
    })
    db.table("extrator_settings").upsert({"key": "shopee_collector", "value": settings}).execute()
    status = "partial" if errors else "completed"  # zero accepted products is still a completed run; explain zero results in details
    details = {
        "platform": "shopee",
        "next_category_cursor": next_cursor,
        "shipping_filter": "national_only_fail_closed",
        "max_new_products": max_new_products,
        "interval_seconds": config.SHOPEE_COLLECTOR_INTERVAL_SECONDS,
        "accepted_national": accepted_national,
        "excluded_international": excluded_international,
        "excluded_unknown_origin": excluded_unknown_origin,
        "product_pages_checked": origin_checks,
        "origin_values_found": origin_values_found,
        "products_without_origin_check": max(0, seen - origin_checks),
        "origin_source": "public_shopee_product_detail",
    }
    db.table("extrator_collection_runs").update({
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "categories_seen": count,
        "items_seen": seen,
        "new_items": new_items,
        "error_count": errors,
        "details": details,
    }).eq("id", run["id"]).execute()
    return {
        "platform": "shopee",
        "categories_seen": count,
        "items_seen": seen,
        "accepted_national": accepted_national,
        "excluded_international": excluded_international,
        "excluded_unknown_origin": excluded_unknown_origin,
        "product_pages_checked": origin_checks,
        "products_without_origin_check": max(0, seen - origin_checks),
        "origin_source": "public_shopee_product_detail",
        "shipping_filter": "national_only_fail_closed",
        "max_new_products": max_new_products,
        "interval_seconds": config.SHOPEE_COLLECTOR_INTERVAL_SECONDS,
        "new_items": new_items,
        "errors": errors,
        "category_errors": category_errors[:5],
        "warning": (
            "A página pública dos anúncios não confirmou a origem nacional nos produtos verificados; nenhum produto de origem desconhecida será enviado."
            if excluded_unknown_origin and accepted_national == 0 else None
        ),
    }
