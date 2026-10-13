"""Teste público da loja Natura, sem login e sem coleta automática."""
import asyncio
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

STORE_URL = "https://www.minhaloja.natura.com/consultoria/lucasuy"
CATALOG_URLS = (
    ("https://www.minhaloja.natura.com/consultoria/lucasuy?marca=natura", "natura"),
    ("https://www.minhaloja.natura.com/consultoria/lucasuy?marca=avon", "avon"),
)


def _with_consultoria(url, marca=None):
    parsed = urlparse(url)
    query = [
        (k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if k.lower() not in ("consultoria", "marca")
    ]
    query.append(("consultoria", "lucasuy"))
    if marca in ("natura", "avon"):
        query.append(("marca", marca))
    else:
        existing_brand = next(
            (v.lower() for k, v in parse_qsl(parsed.query, keep_blank_values=True)
             if k.lower() == "marca" and v.lower() in ("natura", "avon")),
            None,
        )
        if existing_brand:
            query.append(("marca", existing_brand))
    return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))

# Evita duas requisições do painel tentando iniciar o mesmo executável Chromium
# simultaneamente. ETXTBSY é transitório em alguns ambientes Linux/containers.
_BROWSER_LAUNCH_LOCK = asyncio.Lock()


async def _launch_chromium(pw):
    async with _BROWSER_LAUNCH_LOCK:
        for attempt in range(4):
            try:
                return await pw.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-dev-shm-usage"],
                )
            except Exception as exc:
                if "ETXTBSY" not in str(exc) or attempt == 3:
                    raise
                await asyncio.sleep(1.0 * (attempt + 1))


async def _ensure_chromium():
    executable = Path.home() / ".cache" / "ms-playwright"
    if any(executable.glob("chromium_headless_shell-*/chrome-linux/headless_shell")):
        return
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "playwright", "install", "chromium",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    try:
        await asyncio.wait_for(process.communicate(), timeout=180)
    except asyncio.TimeoutError:
        process.kill()
        await process.communicate()
        raise RuntimeError("Tempo esgotado ao instalar o Chromium do Playwright.")
    if process.returncode != 0:
        raise RuntimeError("Não foi possível instalar o Chromium do Playwright.")


def _clean(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _format_price(value):
    value = _clean(value)
    if not value or value.lower() in ("null", "none", "nan"):
        return None
    if re.fullmatch(r"[0-9]+(?:[.,][0-9]{1,2})?", value):
        return "R$ " + value.replace(".", ",") if "," not in value else "R$ " + value
    match = re.search(r"R\$\s*[0-9][0-9.]*,[0-9]{2}", value, re.I)
    return match.group(0) if match else None


def _extract_product_jsonld(raw_items):
    for raw in raw_items:
        try:
            data = json.loads(raw)
        except Exception:
            continue
        candidates = data if isinstance(data, list) else [data]
        for item in candidates:
            if not isinstance(item, dict):
                continue
            if item.get("@type") == "Product" or "offers" in item:
                offers = item.get("offers") or {}
                if isinstance(offers, list):
                    offers = offers[0] if offers else {}
                return {
                    "name": _clean(item.get("name")),
                    "image": item.get("image"),
                    "price": _clean(offers.get("price")),
                    "currency": _clean(offers.get("priceCurrency")) or "BRL",
                    "availability": _clean(offers.get("availability")),
                }
    return {}


def _walk_json(value, product_code, hits):
    if isinstance(value, dict):
        text = json.dumps(value, ensure_ascii=False).lower()
        if product_code.lower() in text:
            hits.append(value)
        for child in value.values():
            _walk_json(child, product_code, hits)
    elif isinstance(value, list):
        for child in value:
            _walk_json(child, product_code, hits)


def _extract_next_payload_product(scripts, product_code):
    """Extrai objetos de produto dos payloads self.__next_f sem depender do DOM."""
    code = product_code.lower()
    candidates = []
    for raw in scripts:
        low = raw.lower()
        if code not in low:
            continue
        # O payload é serializado/escapado; procuramos janelas próximas ao SKU.
        idx = 0
        while True:
            idx = low.find(code, idx)
            if idx < 0:
                break
            window = raw[max(0, idx - 12000): idx + 20000]
            candidates.append(window)
            idx += len(code)

    result = {}
    for raw in candidates:
        decoded = raw.replace('\\\"', '"').replace('\\u0026', '&').replace('\\/', '/')
        pairs = [
            ("image", r'"(?:image|imageUrl|imageURL|url|src)"\s*:\s*"([^"]+)"'),
            ("price", r'"(?:price|salePrice|sellingPrice|currentPrice|promotionalPrice)"\s*:\s*"?([0-9]+(?:[.,][0-9]{1,2})?)"?'),
            ("old_price", r'"(?:listPrice|originalPrice|regularPrice|fullPrice|oldPrice)"\s*:\s*"?([0-9]+(?:[.,][0-9]{1,2})?)"?'),
            ("availability", r'"(?:availability|stockStatus|inventoryStatus|availabilityStatus)"\s*:\s*"([^"]+)"'),
            ("name", r'"(?:name|productName|displayName|title)"\s*:\s*"([^"]+)"')
        ]
        for key, pattern in pairs:
            if result.get(key):
                continue
            m = re.search(pattern, decoded, re.I)
            if m:
                value = _clean(m.group(1))
                if key == "image":
                    if useful_natura_image(value):
                        result[key] = value
                else:
                    result[key] = value
    return result


def useful_natura_image(url):
    value = _clean(url)
    low = value.lower()
    if not value:
        return False
    blocked = (
        "elo.svg", "brand.png", "logo", "favicon", "/commons/",
        "/social/", "sprite", "placeholder", "default-image",
        "visa.", "pix.", "mastercard.", "hipercard.", "amex.",
        "boleto.", "dinners-club.", "bcorp."
    )
    return not any(token in low for token in blocked)


def _extract_embedded_product(raw_items, product_code):
    hits = []
    for raw in raw_items:
        try:
            data = json.loads(raw)
        except Exception:
            continue
        _walk_json(data, product_code, hits)

    result = {}
    for item in hits:
        if not isinstance(item, dict):
            continue
        for key in ("name", "productName", "title", "displayName"):
            if not result.get("name") and isinstance(item.get(key), str):
                result["name"] = _clean(item[key])
        for key in ("price", "salePrice", "sellingPrice", "currentPrice", "promotionalPrice"):
            value = item.get(key)
            if value is not None and not isinstance(value, (dict, list)):
                result.setdefault("price", _clean(value))
        for key in ("listPrice", "originalPrice", "regularPrice", "fullPrice", "oldPrice"):
            value = item.get(key)
            if value is not None and not isinstance(value, (dict, list)):
                result.setdefault("old_price", _clean(value))
        for key in ("image", "imageUrl", "imageURL", "thumbnail", "thumbnailUrl"):
            value = item.get(key)
            if isinstance(value, str) and "logo" not in value.lower():
                result.setdefault("image", value)
        for key in (
            "availability", "stockStatus", "inventoryStatus", "availabilityStatus",
            "status", "sellingStatus"
        ):
            value = item.get(key)
            if isinstance(value, (str, bool, int, float)):
                result.setdefault("availability", _clean(value))
        for key in (
            "available", "inStock", "isAvailable", "isInStock",
            "stock", "stockQuantity", "quantityAvailable"
        ):
            value = item.get(key)
            if isinstance(value, bool):
                result.setdefault("availability", "Disponível" if value else "Indisponível")
            elif isinstance(value, (int, float)) and key != "stock":
                result.setdefault("availability", "Disponível" if value > 0 else "Indisponível")
    return result


def _slug_title(url):
    slug = url.rstrip("/").rsplit("/", 2)[-2]
    slug = re.sub(r"[^a-zA-Z0-9À-ÿ]+", " ", slug)
    return _clean(slug).title()


async def _inspect_product(page, product):
    original_url = product["permalink"]
    official_url = original_url.replace("https://www.minhaloja.natura.com/", "https://www.natura.com.br/", 1)
    await page.goto(original_url, wait_until="domcontentloaded", timeout=30000)
    await page.wait_for_timeout(5000)
    try:
        await page.wait_for_load_state("networkidle", timeout=12000)
    except Exception:
        pass
    try:
        await page.locator("body").wait_for(timeout=5000)
    except Exception:
        pass

    meta = await page.locator("meta").evaluate_all(
        """els => els.map(x => ({
            property: x.getAttribute('property') || '',
            name: x.getAttribute('name') || '',
            content: x.getAttribute('content') || ''
        }))"""
    )
    jsonlds = await page.locator('script[type="application/ld+json"]').all_text_contents()
    scripts = await page.locator("script").all_text_contents()
    body_text = _clean(await page.locator("body").inner_text(timeout=10000))
    # Isola o bloco principal do produto. A página também mostra recomendações
    # com outros preços; eles nunca devem ser usados como preço deste SKU.
    h1_title = ""
    primary_text = body_text
    try:
        h1_title = _clean(await page.locator("h1").first.inner_text(timeout=2500))
    except Exception:
        pass
    if h1_title:
        title_pos = body_text.lower().find(h1_title.lower())
        if title_pos >= 0:
            primary_text = body_text[max(0, title_pos - 120):title_pos + 1800]
    html = await page.content()

    # Captura somente evidências úteis, sem devolver a página inteira.
    product_section = await page.locator("h1").first.evaluate(
        """h1 => {
            const title = (h1.innerText || h1.textContent || '').trim();
            let node = h1;
            let best = h1.parentElement || h1;
            for (let depth = 0; node && depth < 12; depth++, node = node.parentElement) {
                const text = (node.innerText || '').trim();
                const prices = text.match(/R\\$\\s*[0-9][0-9.]*,[0-9]{2}/g) || [];
                if (title && text.includes(title) && prices.length && text.length <= 3500) {
                    best = node;
                    break;
                }
                if (text.length <= 3500 && prices.length) best = node;
            }
            const text = (best.innerText || '').trim();
            const nodes = Array.from(best.querySelectorAll('*')).map(e => ({
                tag: e.tagName,
                text: (e.innerText || e.textContent || '').trim(),
                cls: typeof e.className === 'string' ? e.className : '',
                aria: e.getAttribute('aria-label') || '',
                testid: e.getAttribute('data-testid') || ''
            })).filter(x => /R\\$\\s*\\d/.test(x.text) && x.text.length <= 350);
            return {title, text, nodes};
        }"""
    )
    primary_text = _clean(product_section.get("text") or primary_text)
    price_nodes = product_section.get("nodes") or []
    image_urls = await page.locator("img").evaluate_all(
        """els => els.map(x => ({
            src: x.currentSrc || x.src || x.getAttribute('data-src') || '',
            alt: x.getAttribute('alt') || '',
            cls: typeof x.className === 'string' ? x.className : '',
            width: x.naturalWidth || 0,
            height: x.naturalHeight || 0
        })).filter(x => x.src).slice(0, 30)"""
    )

    meta_map = {}
    for item in meta:
        key = item.get("property") or item.get("name")
        if key and item.get("content"):
            meta_map[key.lower()] = _clean(item["content"])

    product_code = product["permalink"].rstrip("/").rsplit("/", 1)[-1]
    structured = _extract_product_jsonld(jsonlds)
    embedded = _extract_embedded_product(scripts, product_code)
    next_payload = _extract_next_payload_product(scripts, product_code)

    title = (
        structured.get("name")
        or embedded.get("name")
        or meta_map.get("og:title")
        or _slug_title(product["permalink"])
    )
    if title.strip().lower() in ("minha loja", "natura", "minha loja natura"):
        title = _slug_title(product["permalink"])

    candidates = []
    def useful_image(url):
        value = _clean(url)
        low = value.lower()
        if not value:
            return False
        blocked = (
            "elo.svg", "brand.png", "logo", "favicon", "/commons/",
            "/social/", "sprite", "placeholder", "default-image"
        )
        return not any(token in low for token in blocked)

    for value in (
        next_payload.get("image"),
        embedded.get("image"),
        structured.get("image") if isinstance(structured.get("image"), str) else None,
        meta_map.get("og:image"),
    ):
        if useful_image(value):
            candidates.append(value)

    ranked_images = []
    for item in image_urls:
        src = item["src"]
        if not useful_image(src):
            continue
        score = 0
        if item.get("width", 0) >= 300 and item.get("height", 0) >= 300:
            score += 5
        if product_code.lower() in src.lower():
            score += 4
        if "/p/" in src.lower() or "/produto" in src.lower():
            score += 2
        if "natura" in src.lower():
            score += 1
        ranked_images.append((score, src))
    ranked_images.sort(reverse=True)
    candidates.extend(src for _, src in ranked_images)
    image = next(iter(dict.fromkeys(candidates)), None)

    # Preços: prioriza os valores realmente visíveis na página.
    visible_prices = []
    for node in price_nodes:
        text = _clean(node.get("text", ""))
        matches = re.findall(r"R\$\s*[0-9][0-9.]*,[0-9]{2}", text, re.I)
        if matches:
            label = _clean(
                node.get("cls", "") + " " + node.get("testid", "") + " " + node.get("aria", "")
            ).lower()
            score = 0
            if any(k in label for k in ("price", "preco", "preço", "sale", "offer", "valor")):
                score += 5
            if any(k in text.lower() for k in ("por apenas", "por:", "à vista", "preço")):
                score += 4
            if any(k in text.lower() for k in ("de r$", "antes", "era")):
                score -= 2
            visible_prices.append((score, matches, text))

    visible_prices.sort(key=lambda x: x[0], reverse=True)
    price = None
    old_price = None

    # Procura explicitamente a estrutura "de X por Y".
    sale_match = re.search(
        r"(?:de\s*[:]?\s*)?R\$\s*([0-9][0-9.]*,[0-9]{2}).{0,100}?"
        r"R\$\s*([0-9][0-9.]*,[0-9]{2})",
        primary_text, re.I | re.S
    )
    if sale_match:
        old_price = _format_price(sale_match.group(1))
        price = _format_price(sale_match.group(2))

    if not price and visible_prices:
        ranked_visible = sorted(visible_prices, key=lambda x: (x[0], -len(x[2])), reverse=True)
        for score, matches, node_text in ranked_visible:
            low_text = node_text.lower()
            if any(k in low_text for k in ("frete", "parcel", "pix", "cartão", "cartao")):
                continue
            price = matches[-1]
            break

    # Payload estruturado somente se estiver ligado ao SKU consultado.
    if not price:
        for candidate in (next_payload.get("price"), embedded.get("price"), structured.get("price")):
            formatted = _format_price(candidate)
            if formatted:
                price = formatted
                break
    if not old_price:
        old_price = (
            _format_price(next_payload.get("old_price"))
            or _format_price(embedded.get("old_price"))
        )

    raw_price_matches = list(dict.fromkeys(
        re.findall(r"R\$\s*[0-9][0-9.]*,[0-9]{2}", body_text, re.I)
    ))

    # Segurança: em uma promoção normal, o preço anterior não pode ser menor
    # que o preço atual. Corrige inversão proveniente do payload.
    if price and old_price:
        try:
            current_value = float(re.sub(r"[^0-9,]", "", price).replace(".", "").replace(",", "."))
            old_value = float(re.sub(r"[^0-9,]", "", old_price).replace(".", "").replace(",", "."))
            if old_value < current_value:
                price, old_price = old_price, price
        except Exception:
            pass

    # Usa preço visível somente como fallback do preço atual.
    # Não transforma outro valor da página em preço anterior sem evidência estruturada.
    if not price:
        preferred_nodes = [
            x for x in price_nodes
            if any(token in (x.get("cls", "") + " " + x.get("testid", "") + " " + x.get("aria", "")).lower()
                   for token in ("price", "preco", "preço", "valor", "offer", "sale"))
        ]
        node_prices = []
        for node in preferred_nodes:
            node_prices.extend(re.findall(r"R\$\s*[0-9][0-9.]*,[0-9]{2}", node.get("text", ""), re.I))
        if node_prices:
            price = node_prices[-1]
        elif len(raw_price_matches) == 1:
            price = raw_price_matches[0]

    availability = next_payload.get("availability") or embedded.get("availability") or structured.get("availability")
    if not availability:
        low = body_text.lower()
        for word in ("disponível", "indisponível", "esgotado", "sem estoque"):
            if word in low:
                availability = word
                break

    discount = None
    discount_match = re.search(
        r"(?:R\\$\\s*[0-9][0-9.]*,[0-9]{2}.{0,100}?-\\s*(\\d{1,2})\\s*%|"
        r"-\\s*(\\d{1,2})\\s*%.{0,100}?R\\$\\s*[0-9][0-9.]*,[0-9]{2})",
        primary_text, re.I | re.S
    )
    if discount_match:
        discount = (discount_match.group(1) or discount_match.group(2)) + "%"
    elif price and old_price:
        try:
            current = float(re.sub(r"[^0-9,]", "", price).replace(".", "").replace(",", "."))
            original = float(re.sub(r"[^0-9,]", "", old_price).replace(".", "").replace(",", "."))
            if original > current:
                discount = str(round((1 - current / original) * 100)) + "%"
        except Exception:
            pass

    # Diagnóstico real da página, para orientar o próximo parser.
    debug_snippets = []
    for pattern in (
        r".{0,100}R\$\s*[0-9][0-9.]*,[0-9]{2}.{0,100}",
        r".{0,100}(?:preço|preco|por apenas|de\s+R\$|à vista).{0,140}",
    ):
        for match in re.findall(pattern, body_text, re.I):
            value = _clean(match)
            if value and value not in debug_snippets:
                debug_snippets.append(value)
            if len(debug_snippets) >= 10:
                break
        if len(debug_snippets) >= 10:
            break

    debug_scripts = []
    for raw in scripts:
        if product_code.lower() in raw.lower() and any(
            key in raw.lower()
            for key in ("price", "preco", "preço", "image", "media", "offer")
        ):
            compact = _clean(raw)
            if compact:
                debug_scripts.append(compact[:1000])
            if len(debug_scripts) >= 5:
                break

    # Fallback: a loja personalizada pode não expor os dados comerciais no DOM.
    # Consulta a página oficial do mesmo SKU, sem alterar o link publicado.
    if official_url != original_url and (not price or not old_price):
        try:
            await page.goto(official_url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(3500)
            try:
                await page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
            official_body = _clean(await page.locator("body").inner_text(timeout=10000))
            official_nodes = await page.locator("body *").evaluate_all(
                """els => els.map(e => ({text: (e.innerText || e.textContent || '').trim(), cls: typeof e.className === 'string' ? e.className : '', aria: e.getAttribute('aria-label') || '', testid: e.getAttribute('data-testid') || ''})).filter(x => /R\\$\\s*\\d/.test(x.text) && x.text.length <= 500).slice(0, 80)"""
            )
            official_prices = []
            for node in official_nodes:
                text = _clean(node.get("text", ""))
                matches = re.findall(r"R\$\s*[0-9][0-9.]*,[0-9]{2}", text, re.I)
                if matches:
                    label = _clean(node.get("cls", "") + " " + node.get("testid", "") + " " + node.get("aria", "")).lower()
                    score = 0
                    if any(k in label for k in ("price", "preco", "preço", "sale", "offer", "valor")):
                        score += 5
                    if len(matches) <= 2:
                        score += 1
                    official_prices.append((score, matches, text))
            official_prices.sort(key=lambda x: (x[0], -len(x[2])), reverse=True)
            sale = re.search(r"de\s*[:]?\s*R\$\s*([0-9][0-9.]*,[0-9]{2}).{0,120}?(?:por|agora|à vista)\s*[:]?\s*R\$\s*([0-9][0-9.]*,[0-9]{2})", official_body, re.I | re.S)
            if sale:
                if not old_price:
                    old_price = _format_price(sale.group(1))
                if not price:
                    price = _format_price(sale.group(2))
            elif official_prices:
                matches = official_prices[0][1]
                if len(matches) >= 2:
                    if not old_price:
                        old_price = _format_price(matches[0])
                    if not price:
                        price = _format_price(matches[-1])
                elif not price:
                    price = _format_price(matches[-1])
            low_official = official_body.lower()
            if not availability:
                if any(x in low_official for x in ("indisponível", "esgotado", "sem estoque")):
                    availability = "Indisponível"
                elif "adicionar à sacola" in low_official or "adicionar a sacola" in low_official:
                    availability = "Disponível"
            if price:
                price_source = "official_natura_product_page"
        except Exception as exc:
            debug_snippets.append(f"Fallback oficial Natura: {type(exc).__name__}: {str(exc)[:160]}")

    if price and old_price:
        try:
            current = float(re.sub(r"[^0-9,]", "", price).replace(".", "").replace(",", "."))
            original = float(re.sub(r"[^0-9,]", "", old_price).replace(".", "").replace(",", "."))
            if original > current:
                discount = str(round((1 - current / original) * 100)) + "%"
        except Exception:
            pass

    availability_text = _clean(availability)
    if availability_text:
        availability_low = availability_text.lower()
        if "schema.org/instock" in availability_low or availability_low in ("instock", "in stock", "available", "true"):
            availability_text = "Disponível"
        elif any(token in availability_low for token in ("outofstock", "soldout", "indisponível", "esgotado")):
            availability_text = "Indisponível"
        elif availability_low.startswith("https://schema.org/") or availability_low.startswith("http://schema.org/"):
            availability_text = None

    product.update({
        "title": _clean(title)[:180] or "Produto Natura",
        "price": price,
        "old_price": old_price,
        "discount": discount,
        "image": image,
        "availability": availability_text,
        "details_verified": bool(price and old_price and image),
        "price_source": (
            price_source if "price_source" in locals() else
            "primary_product_text" if sale_match else
            "visible_product_price" if visible_prices and price else
            "structured_product_payload" if price else
            None
        ),
    })
    return product


async def collect_natura_public_once(max_products=None, inspect_details=True):
    products, seen, errors = [], set(), []
    await _ensure_chromium()

    async with async_playwright() as pw:
        browser = await _launch_chromium(pw)
        context = await browser.new_context(
            locale="pt-BR", timezone_id="America/Sao_Paulo",
            viewport={"width": 1365, "height": 900},
        )
        page = await context.new_page()
        try:
            # Varre os catálogos Natura e Avon separadamente para que cada URL
            # de produto mantenha a marca e a atribuição da consultoria.
            queue = list(CATALOG_URLS)
            category_queue = []
            product_queue = []
            visited_pages = set()
            seen_skus = set()
            queued_products = set()
            max_pages_to_crawl = 60
            excluded_paths = (
                "/login", "/account", "/checkout", "/carrinho",
                "/cart", "/favoritos", "/wishlist", "/ajuda", "/atendimento",
            )

            async def collect_links_from_current_page(current_marca):
                anchors = await page.locator("a").evaluate_all(
                    """els => els.map(a => ({
                        href: a.href || a.getAttribute('href') || '',
                        text: (a.innerText || a.textContent || '').trim()
                    }))"""
                )
                for item in anchors:
                    raw_href = str(item.get("href") or "").split("#")[0]
                    parsed = urlparse(raw_href)
                    if parsed.scheme != "https" or parsed.netloc.lower() != "www.minhaloja.natura.com":
                        continue
                    parsed_path = parsed.path.rstrip("/") or "/"
                    if parsed_path.lower().startswith("/p/"):
                        product_match = re.fullmatch(r"/p/.+/((?:NATBRA|AVNBRA)-\d+)", parsed_path, re.I)
                        if product_match:
                            sku = product_match.group(1).upper()
                            canonical_url = _with_consultoria(
                                "https://www.minhaloja.natura.com" + parsed_path, current_marca
                            )
                            if sku not in seen_skus and (max_products is None or len(products) < max_products):
                                seen_skus.add(sku)
                                products.append({
                                    "title": _clean(item.get("text"))[:180] or _slug_title(canonical_url),
                                    "permalink": canonical_url,
                                    "brand_catalog": current_marca,
                                    "sku": sku,
                                })
                            product_key = (sku, current_marca)
                            if product_key not in queued_products:
                                queued_products.add(product_key)
                                product_queue.append((canonical_url, current_marca))
                        continue

                    low_path = parsed_path.lower()
                    if any(token in low_path for token in excluded_paths):
                        continue
                    # Não voltar à rota genérica de consultoria: ela perde a marca
                    # selecionada e pode causar ciclos entre catálogos.
                    if low_path.startswith("/consultoria/") or parsed_path == "/":
                        continue
                    if not parsed_path.startswith("/"):
                        continue
                    candidate = _with_consultoria(
                        "https://www.minhaloja.natura.com" + parsed_path, current_marca
                    )
                    if (candidate, current_marca) not in visited_pages and all(
                        candidate != queued_url for queued_url, _ in queue
                    ) and all(candidate != queued_url for queued_url, _ in category_queue) and all(
                        candidate != queued_url for queued_url, _ in product_queue
                    ):
                        category_queue.append((candidate, current_marca))

            while (queue or category_queue or product_queue) and len(visited_pages) < max_pages_to_crawl and (max_products is None or len(products) < max_products):
                if queue:
                    current_url, current_marca = queue.pop(0)
                elif category_queue:
                    current_url, current_marca = category_queue.pop(0)
                else:
                    current_url, current_marca = product_queue.pop(0)
                page_key = (current_url, current_marca)
                if page_key in visited_pages:
                    continue
                visited_pages.add(page_key)
                try:
                    await page.goto(current_url, wait_until="domcontentloaded", timeout=25000)
                    await page.wait_for_timeout(1800 if current_url in (u for u, _ in CATALOG_URLS) else 1100)
                    await collect_links_from_current_page(current_marca)
                except PlaywrightTimeoutError:
                    errors.append("Tempo esgotado ao abrir página para descobrir links: " + current_url)
                except Exception as exc:
                    errors.append(f"Descoberta de links: {type(exc).__name__}: {str(exc)[:140]}")

            if inspect_details:
                for product in products:
                    try:
                        await _inspect_product(page, product)
                    except PlaywrightTimeoutError:
                        product["details_verified"] = False
                        errors.append("Tempo esgotado ao consultar: " + product["permalink"])
                    except Exception as exc:
                        product["details_verified"] = False
                        errors.append(f"Detalhes Natura: {type(exc).__name__}: {str(exc)[:180]}")

        except PlaywrightTimeoutError:
            errors.append("Tempo esgotado ao abrir a loja pública Natura.")
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {str(exc)[:250]}")
        finally:
            await context.close()
            await browser.close()

    return {
        "source": "natura_public_browser",
        "store_url": STORE_URL,
        "catalog_urls": [url for url, _ in CATALOG_URLS],
        "products_discovered": len(products),
        "products": products,
        "errors": errors,
        "automatic_collection_enabled": False,
    }


async def inspect_natura_product_url(url):
    """Consulta um link de produto Natura e devolve os dados somente ao painel."""
    raw_url = _clean(url)
    if not raw_url:
        raise ValueError("Cole o link de um produto Natura.")
    if not re.match(r"^https?://", raw_url, re.I):
        raw_url = "https://" + raw_url
    parsed = urlparse(raw_url)
    host = (parsed.hostname or "").lower()
    allowed_hosts = {"minhaloja.natura.com", "www.minhaloja.natura.com", "natura.com.br", "www.natura.com.br"}
    if parsed.scheme not in ("http", "https") or host not in allowed_hosts or parsed.username or parsed.password:
        raise ValueError("Use um link público de produto dos domínios minhaloja.natura.com ou natura.com.br.")
    path = (parsed.path or "").rstrip("/")
    if not path or path.lower() in ("/", "/consultoria/lucasuy"):
        raise ValueError("O link informado parece ser a página inicial da loja, não um produto.")
    product = {"title": "Produto Natura", "permalink": raw_url}
    await _ensure_chromium()
    async with async_playwright() as pw:
        browser = await _launch_chromium(pw)
        context = await browser.new_context(locale="pt-BR", timezone_id="America/Sao_Paulo", viewport={"width": 1365, "height": 900})
        page = await context.new_page()
        try:
            try:
                await _inspect_product(page, product)
            except PlaywrightTimeoutError:
                product["details_verified"] = False
                product["error"] = "Tempo esgotado ao consultar a página do produto."
            return {
                "source": "natura_product_link",
                "product": product,
                "automatic_collection_enabled": False,
                "telegram_sent": False,
            }
        finally:
            await context.close()
            await browser.close()
