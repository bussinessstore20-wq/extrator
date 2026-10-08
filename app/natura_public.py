"""Teste público da loja Natura, sem login e sem coleta automática."""
import asyncio
import json
import re
import sys
from pathlib import Path
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

STORE_URL = "https://www.minhaloja.natura.com/consultoria/lucasuy"


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
    await page.goto(product["permalink"], wait_until="domcontentloaded", timeout=30000)
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
    html = await page.content()

    # Captura somente evidências úteis, sem devolver a página inteira.
    price_nodes = await page.locator("body *").evaluate_all(
        """els => els.map(e => ({
            tag: e.tagName,
            text: (e.innerText || e.textContent || '').trim(),
            cls: typeof e.className === 'string' ? e.className : '',
            aria: e.getAttribute('aria-label') || '',
            testid: e.getAttribute('data-testid') || ''
        })).filter(x => /R\\$\\s*\\d/.test(x.text)).slice(0, 30)"""
    )
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
        next_payload.get("image"),\n        embedded.get("image"),
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

    price = _format_price(next_payload.get("price")) or _format_price(embedded.get("price")) or _format_price(structured.get("price"))
    old_price = _format_price(next_payload.get("old_price")) or _format_price(embedded.get("old_price"))

    raw_price_matches = list(dict.fromkeys(
        re.findall(r"R\$\s*[0-9][0-9.]*,[0-9]{2}", body_text + "\n" + html, re.I)
    ))

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
        elif raw_price_matches:
            price = raw_price_matches[-1]

    availability = next_payload.get("availability") or embedded.get("availability") or structured.get("availability")
    if not availability:
        low = body_text.lower()
        for word in ("disponível", "indisponível", "esgotado", "sem estoque"):
            if word in low:
                availability = word
                break

    discount = None
    discount_match = re.search(r"(\d{1,2})\s*%\s*(?:off|de desconto)", body_text, re.I)
    if discount_match:
        discount = discount_match.group(1) + "%"
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

    product.update({
        "title": _clean(title)[:180] or "Produto Natura",
        "price": price,
        "old_price": old_price,
        "discount": discount,
        "image": image,
        "availability": availability,
        "details_verified": True,
        "debug_price_nodes": price_nodes[:10],
        "debug_images": [{"score": score, "src": src} for score, src in ranked_images[:15]],
        "debug_snippets": debug_snippets[:10],
        "debug_product_scripts": debug_scripts,
    })
    return product


async def collect_natura_public_once(max_products=10):
    products, seen, errors = [], set(), []
    await _ensure_chromium()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        context = await browser.new_context(
            locale="pt-BR", timezone_id="America/Sao_Paulo",
            viewport={"width": 1365, "height": 900},
        )
        page = await context.new_page()
        try:
            await page.goto(STORE_URL, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(5000)
            for _ in range(4):
                await page.mouse.wheel(0, 1800)
                await page.wait_for_timeout(1000)

            anchors = await page.locator("a").evaluate_all(
                """els => els.map(a => {
                    const card = a.closest(
                        '[data-testid*="product"], [class*="product"], [class*="Product"], article, li'
                    );
                    return {
                        href: a.href || a.getAttribute('href') || '',
                        text: (a.innerText || a.textContent || '').trim(),
                        cardText: card ? (card.innerText || card.textContent || '').trim() : ''
                    };
                })"""
            )

            blocked = (
                "/carrinho", "/login", "/entrar", "/cadastro",
                "/favoritos", "/checkout", "/busca"
            )

            for item in anchors:
                href = str(item.get("href") or "").split("#")[0].split("?")[0]
                text = _clean(item.get("text"))
                card_text = _clean(item.get("cardText"))
                if not href.startswith("https://www.minhaloja.natura.com/"):
                    continue
                if href.rstrip("/") == STORE_URL.rstrip("/"):
                    continue
                lower = href.lower()
                if any(part in lower for part in blocked):
                    continue

                path = href.split("minhaloja.natura.com/", 1)[-1].strip("/")
                if not path or path.startswith("c/"):
                    continue

                has_price = bool(re.search(r"r\$\s*[0-9][0-9.,]*", card_text, re.I))
                product_hint = bool(re.search(r"/(produto|produtos|item|itens)(/|$)", "/" + path, re.I))
                deep_store_path = path.startswith("consultoria/lucasuy/") and path.count("/") >= 2
                if not (has_price or product_hint or deep_store_path):
                    continue
                if href in seen:
                    continue

                seen.add(href)
                products.append({
                    "title": text[:180] or "Produto Natura",
                    "permalink": href,
                })
                if len(products) >= max_products:
                    break

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
        "products_discovered": len(products),
        "products": products,
        "errors": errors,
        "automatic_collection_enabled": False,
    }
