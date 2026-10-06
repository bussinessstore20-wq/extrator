"""Shopee Affiliate Open API collector with public product-page origin verification and import-notice filtering.

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

# Discovered once per process from the account's GraphQL schema.
_AFFILIATE_ORIGIN_FIELDS = None
_AFFILIATE_SCHEMA_DIAGNOSTIC = None


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


def inspect_international_evidence(page_html: str) -> dict:
    """Return product-specific international evidence only.

    Generic tax/import tooltips are not product-level evidence: Shopee can
    ship the same UI strings for unrelated listings. Only an explicit
    product shipping label is accepted here.
    """
    decoded = html.unescape(page_html or "")
    decoded = decoded.replace("\\u00e7", "ç").replace("\\u00e3", "ã")
    decoded = re.sub(
        r"<(script|style|noscript|template|svg)\\b[^>]*>.*?</\\1\\s*>",
        " ",
        decoded,
        flags=re.IGNORECASE | re.DOTALL,
    )
    decoded = re.sub(r"<!--.*?-->", " ", decoded, flags=re.DOTALL)
    visible = re.sub(r"<[^>]+>", " ", decoded)
    visible = re.sub(r"\\s+", " ", visible).strip()
    normalized = _normalise_origin(visible)
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized)

    # Do NOT use the generic import/tax tooltip as evidence.
    # It was proven to occur in unrelated product pages.
    exterior_match = re.search(r"\\benviado\\s+de\\s+(?:o\\s+)?exterior\\b", normalized)
    if not exterior_match:
        exterior_match = re.search(r"\\bshipping\\s+from\\s+(?:the\\s+)?exterior\\b", normalized)

    evidence = None
    evidence_type = None
    if exterior_match:
        start = max(0, exterior_match.start() - 120)
        end = min(len(normalized), exterior_match.end() + 180)
        evidence = normalized[start:end]
        evidence_type = "shipping_from_exterior_visible"

    return {
        "international": bool(evidence_type),
        "evidence_type": evidence_type,
        "evidence": evidence,
        "visible_text_length": len(normalized),
    }

def has_international_import_notice(page_html: str) -> bool:
    return inspect_international_evidence(page_html)["international"]


def inspect_product_page(page_html: str) -> dict:
    """Read origin plus auditable visible evidence from the product page."""
    international = inspect_international_evidence(page_html)
    return {
        "origin": extract_shipping_origin(page_html),
        **international,
    }


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
    if metadata.get("shipping_is_international") is True:
        return False, "international"

    # The affiliate offer includes shopType values such as SHOPEE_MALL_CB /
    # C2C_CB and their *_NON_CB counterparts. Use only explicit typed values;
    # never infer origin from the seller's shop address or missing fields.
    raw_shop_type = metadata.get("shop_type")
    if isinstance(raw_shop_type, str):
        shop_types = [raw_shop_type.upper()]
    elif isinstance(raw_shop_type, (list, tuple)):
        shop_types = [str(value).upper() for value in raw_shop_type]
    else:
        shop_types = []
    shop_types = [value.strip() for value in shop_types if value and str(value).strip()]
    if shop_types:
        has_cb = any(value.endswith("_CB") and not value.endswith("_NON_CB") for value in shop_types)
        has_non_cb = any(value.endswith("_NON_CB") for value in shop_types)
        if has_cb:
            return False, "international"
        if has_non_cb:
            return True, "national_confirmed_offer_type"

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


def extract_structured_shipping_origin(payload: dict) -> str | None:
    """Read explicit shipping-origin fields from Shopee's structured product response."""
    found = []

    def walk(value, key=""):
        if isinstance(value, dict):
            for child_key, child_value in value.items():
                walk(child_value, str(child_key).lower())
        elif isinstance(value, list):
            for child in value:
                walk(child, key)
        elif isinstance(value, str):
            key_normalized = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")
            # Do not use shop_location: seller address is not proof of dispatch origin.
            allowed = (
                "shipping_from", "shipping_origin", "ship_from",
                "origin_location", "dispatch_origin", "warehouse_location",
                "shipping_address_label",
            )
            if any(token in key_normalized for token in allowed):
                value = value.strip()
                if value and len(value) <= 100:
                    found.append(value)
            match = re.search(
                r"(?:Enviado\s+de|Shipping\s+from)\s*:?\s*"
                r"([A-Za-zÀ-ÿ][A-Za-zÀ-ÿ .,'-]{1,55})",
                value,
                flags=re.IGNORECASE,
            )
            if match:
                found.append(match.group(1).strip())

    walk(payload)
    for origin in found:
        normalized = _normalise_origin(origin)
        if any(_normalise_origin(city) in normalized for city in BRAZILIAN_CITIES) or any(
            re.search(r"\b" + re.escape(_normalise_origin(state)) + r"\b", normalized)
            for state in BRAZILIAN_STATES
        ):
            return origin
        if any(_normalise_origin(country) in normalized for country in FOREIGN_ORIGINS):
            return origin
    return found[0] if found else None


async def fetch_shipping_origin(item: dict, semaphore: asyncio.Semaphore) -> dict:
    """Check the product page and Shopee's public product-detail JSON endpoint."""
    metadata = item.get("metadata") or {}
    product_url = str(metadata.get("product_link") or "").strip()
    parsed = urlparse(product_url)
    if parsed.scheme != "https" or parsed.hostname not in {"shopee.com.br", "www.shopee.com.br"}:
        return {"origin": None, "international": False}

    shop_id = str(metadata.get("shop_id") or "").strip()
    item_id = str(metadata.get("item_id") or "").strip()
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0.0.0 Safari/537.36",
        "Accept": "text/html,application/json",
        "Referer": "https://shopee.com.br/",
        "X-Requested-With": "XMLHttpRequest",
    }
    async with semaphore:
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(8.0, connect=3.0),
                follow_redirects=True,
                headers=headers,
            ) as client:
                response = await client.get(product_url)
                final_host = urlparse(str(response.url)).hostname
                page_result = {"origin": None, "international": False}
                if response.status_code < 400 and final_host in {"shopee.com.br", "www.shopee.com.br"}:
                    page_result = inspect_product_page(response.text)

                # Shopee renders parts of the product page dynamically. If the HTML
                # did not expose the notice/origin, query its public product-detail JSON.
                if shop_id.isdigit() and item_id.isdigit() and (
                    not page_result["origin"] or not page_result["international"]
                ):
                    detail_url = (
                        "https://shopee.com.br/api/v4/pdp/get_pc"
                        f"?shop_id={shop_id}&item_id={item_id}&tz_offset_minutes=-180&detail_level=0"
                    )
                    try:
                        detail = await client.get(detail_url)
                        if detail.status_code < 400:
                            payload = detail.json()
                            # Do not classify by scanning the entire JSON response for a warning:
                            # generic/help text in payloads is not reliable product-level evidence.
                            if not page_result["origin"]:
                                page_result["origin"] = extract_structured_shipping_origin(payload)
                    except (httpx.HTTPError, ValueError):
                        logger.info("Endpoint estruturado de produto Shopee indisponível para item %s", item_id)
                return page_result
        except (httpx.HTTPError, ValueError) as exc:
            logger.info("Não foi possível confirmar origem no anúncio Shopee: %s", type(exc).__name__)
            return {"origin": None, "international": False}

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


def _unwrap_named_type(type_info: dict | None) -> str | None:
    current = type_info or {}
    for _ in range(6):
        if current.get("name"):
            return str(current["name"])
        current = current.get("ofType") or {}
    return None


def _unwrap_type_kind(type_info: dict | None) -> str:
    current = type_info or {}
    for _ in range(6):
        kind = str(current.get("kind") or "").upper()
        if kind in {"SCALAR", "ENUM", "OBJECT", "INPUT_OBJECT", "INTERFACE", "UNION"}:
            return kind
        current = current.get("ofType") or {}
    return ""


async def inspect_affiliate_schema() -> dict:
    """Discover scalar/enum product-offer fields exposed by this API account."""
    global _AFFILIATE_ORIGIN_FIELDS, _AFFILIATE_SCHEMA_DIAGNOSTIC
    if _AFFILIATE_SCHEMA_DIAGNOSTIC is not None:
        return _AFFILIATE_SCHEMA_DIAGNOSTIC

    probe = """
    query {
      __schema {
        queryType {
          fields {
            name
            type {
              kind
              name
              ofType {
                kind
                name
                ofType {
                  kind
                  name
                  ofType {
                    kind
                    name
                  }
                }
              }
            }
          }
        }
      }
    }
    """
    try:
        data = await graphql(probe)
        query_fields = ((data.get("__schema") or {}).get("queryType") or {}).get("fields") or []
        product_field = next(
            (field for field in query_fields if field.get("name") == "productOfferV2"),
            None,
        )
        connection_type = _unwrap_named_type((product_field or {}).get("type"))
        if not connection_type:
            raise RuntimeError("o schema não informou o tipo de retorno de productOfferV2")

        connection_probe = f'''
        query {{
          __type(name: {_gql_string(connection_type)}) {{
            name
            fields {{
              name
              type {{
                kind
                name
                ofType {{ kind name }}
              }}
            }}
          }}
        }}
        '''
        connection_data = await graphql(connection_probe)
        connection_fields = ((connection_data.get("__type") or {}).get("fields") or [])
        nodes_field = next(
            (field for field in connection_fields if field.get("name") == "nodes"),
            None,
        )
        node_type = _unwrap_named_type((nodes_field or {}).get("type"))
        if not node_type:
            raise RuntimeError("o schema não informou o tipo dos nodes de productOfferV2")

        detail_probe = f'''
        query {{
          __type(name: {_gql_string(node_type)}) {{
            name
            fields {{
              name
              type {{
                kind
                name
                ofType {{ kind name }}
              }}
            }}
          }}
        }}
        '''
        detail = await graphql(detail_probe)
        fields = (detail.get("__type") or {}).get("fields") or []
        keywords = (
            "cross", "ship", "origin", "warehouse", "country", "location",
            "dispatch", "logistic", "delivery",
        )
        relevant = sorted({
            str(field.get("name"))
            for field in fields
            if field.get("name")
            and any(keyword in str(field.get("name")).lower() for keyword in keywords)
            and _unwrap_type_kind(field.get("type")) in {"SCALAR", "ENUM"}
        })
        _AFFILIATE_ORIGIN_FIELDS = relevant
        _AFFILIATE_SCHEMA_DIAGNOSTIC = {
            "introspection": "ok",
            "product_offer_type": connection_type,
            "product_offer_node_type": node_type,
            "origin_related_fields": relevant,
            "origin_related_field_count": len(relevant),
        }
    except Exception as exc:
        _AFFILIATE_ORIGIN_FIELDS = []
        _AFFILIATE_SCHEMA_DIAGNOSTIC = {
            "introspection": "failed",
            "error": str(exc)[:250],
            "origin_related_fields": [],
            "origin_related_field_count": 0,
        }
    return _AFFILIATE_SCHEMA_DIAGNOSTIC


def _build_product_offer_query(keyword: str, limit: int, extra_fields: list[str]) -> str:
    base_fields = [
        "itemId", "productName", "productLink", "offerLink", "imageUrl",
        "priceMin", "priceMax", "priceDiscountRate", "sales", "ratingStar",
        "commissionRate", "sellerCommissionRate", "shopeeCommissionRate",
        "shopId", "shopName", "shopType",
    ]
    fields = []
    for field in base_fields + extra_fields:
        if field not in fields:
            fields.append(field)
    return (
        "query { productOfferV2("
        f"keyword: {keyword}, listType: 0, sortType: 1, page: 1, limit: {limit}"
        ") { nodes { " + " ".join(fields) + " } "
        "pageInfo { page limit hasNextPage } } }"
    )


async def search_category(category: dict) -> tuple[list[dict], dict]:
    keyword = _gql_string(str(category.get("keyword") or category.get("name") or "ofertas"))
    limit = min(50, max(1, config.SHOPEE_MAX_ITEMS_PER_CATEGORY))
    schema = await inspect_affiliate_schema()
    extra_fields = list(_AFFILIATE_ORIGIN_FIELDS or [])[:25]
    query = _build_product_offer_query(keyword, limit, extra_fields)
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
        api_origin_data = {
            field: node.get(field)
            for field in extra_fields
            if node.get(field) not in (None, "", [], {})
        }
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
                "api_origin_fields": api_origin_data,
            },
        })
    return items, schema


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
    international_notices_found = 0
    origin_check_limit = 50
    # Diagnostic snapshot of the actual affiliate API fields. Values are limited and
    # aggregated so the test can reveal whether these fields are strings, lists, or numeric enums.
    offer_type_diagnostics = {"shop_type": {}, "api_origin_fields": {}}
    _evidence_samples = []
    _unknown_origin_samples = []
    def record_offer_value(field, value):
        if value is None:
            label = "null"
        elif isinstance(value, bool):
            label = f"bool:{value}"
        elif isinstance(value, (int, float, str)):
            label = f"{type(value).__name__}:{str(value)[:70]}"
        elif isinstance(value, (list, tuple)):
            label = f"{type(value).__name__}:" + json.dumps(value, ensure_ascii=False, default=str)[:100]
        elif isinstance(value, dict):
            label = "dict:" + ",".join(str(k)[:25] for k in list(value.keys())[:8])
        else:
            label = type(value).__name__
        bucket = offer_type_diagnostics[field]
        bucket[label] = bucket.get(label, 0) + 1
    origin_semaphore = asyncio.Semaphore(10)
    for category in selected:
        if new_items >= max_new_products or origin_checks >= origin_check_limit:
            break
        try:
            items, schema_diagnostic = await search_category(category)
            seen += len(items)
            candidates = items[: max(0, origin_check_limit - origin_checks)]
            origins = await asyncio.gather(
                *(fetch_shipping_origin(item, origin_semaphore) for item in candidates),
                return_exceptions=True,
            )
            origin_checks += len(candidates)
            for item, page_result in zip(candidates, origins):
                metadata = item.get("metadata") or {}
                record_offer_value("shop_type", metadata.get("shop_type"))
                for field_name, field_value in (metadata.get("api_origin_fields") or {}).items():
                    record_offer_value(f"api:{field_name}", field_value)
                if isinstance(page_result, dict):
                    page_origin = page_result.get("origin")
                    evidence_type = str(page_result.get("evidence_type") or "").strip()
                    evidence_text = str(page_result.get("evidence") or "").strip()
                    # International exclusion is allowed only when the page returned
                    # an explicit, auditable visible-page evidence record.
                    is_international = (
                        page_result.get("international") is True
                        and bool(evidence_type)
                        and bool(evidence_text)
                    )
                    item.setdefault("metadata", {})["shipping_is_international"] = is_international
                    if is_international:
                        international_notices_found += 1
                        item.setdefault("metadata", {})["international_evidence_type"] = page_result.get("evidence_type")
                        item.setdefault("metadata", {})["international_evidence"] = page_result.get("evidence")
                        if len(_evidence_samples) < 5:
                            _evidence_samples.append({
                                "item_id": item.get("id"),
                                "evidence_type": evidence_type,
                                "evidence": evidence_text,
                            })
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
                        if len(_unknown_origin_samples) < 10:
                            _unknown_origin_samples.append({
                                "item_id": item.get("id"),
                                "title": str(item.get("title") or "").strip()[:180],
                                "permalink": str(item.get("permalink") or "").strip(),
                            })
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
        "international_notices_found": international_notices_found,
        "international_evidence_samples": _evidence_samples,
        "unknown_origin_samples": _unknown_origin_samples,
        "unknown_origin_links": unknown_origin_links,
        "products_without_origin_check": max(0, seen - origin_checks),
        "origin_source": "public_shopee_product_detail",
        "offer_type_diagnostics": offer_type_diagnostics,
        "affiliate_schema": _AFFILIATE_SCHEMA_DIAGNOSTIC,
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
    unknown_origin_links = [
        str(sample.get("permalink") or "").strip()
        for sample in _unknown_origin_samples
        if str(sample.get("permalink") or "").strip()
    ][:10]
    return {
        "platform": "shopee",
        "categories_seen": count,
        "items_seen": seen,
        "accepted_national": accepted_national,
        "excluded_international": excluded_international,
        "excluded_unknown_origin": excluded_unknown_origin,
        "product_pages_checked": origin_checks,
        "origin_values_found": origin_values_found,
        "international_notices_found": international_notices_found,
        "international_evidence_samples": _evidence_samples,
        "unknown_origin_samples": _unknown_origin_samples,
        "products_without_origin_check": max(0, seen - origin_checks),
        "origin_source": "public_shopee_product_detail",
        "offer_type_diagnostics": offer_type_diagnostics,
        "affiliate_schema": _AFFILIATE_SCHEMA_DIAGNOSTIC,
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
