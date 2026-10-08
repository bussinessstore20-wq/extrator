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


def _first_price(text):
    match = re.search(r"R\$\s*([0-9][0-9.]*,[0-9]{2})", text or "", re.I)
    return "R$ " + match.group(1) if match else None


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


async def _inspect_product(page, product):
    await page.goto(product["permalink"], wait_until="domcontentloaded", timeout=30000)
    await page.wait_for_timeout(2200)

    meta = await page.locator('meta').evaluate_all(
        """els => els.map(x => ({
            property: x.getAttribute('property') || '',
            name: x.getAttribute('name') || '',
            content: x.getAttribute('content') || ''
        }))"""
    )
    jsonlds = await page.locator('script[type="application/ld+json"]').all_text_contents()
    body_text = _clean(await page.locator("body").inner_text(timeout=10000))

    meta_map = {}
    for item in meta:
        key = item.get("property") or item.get("name")
        if key and item.get("content"):
            meta_map[key.lower()] = _clean(item["content"])

    structured = _extract_product_jsonld(jsonlds)
    title = structured.get("name") or meta_map.get("og:title") or product.get("title")
    image = structured.get("image")
    if isinstance(image, list):
        image = image[0] if image else None
    image = image or meta_map.get("og:image")

    price = None
    if structured.get("price"):
        price = "R$ " + structured["price"].replace(".", ",") if re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", structured["price"]) else structured["price"]
    if not price:
        price = _first_price(body_text)

    old_price = None
    price_candidates = re.findall(r"R\$\s*[0-9][0-9.]*,[0-9]{2}", body_text, re.I)
    if len(price_candidates) >= 2 and price and price_candidates[0] != price:
        old_price = price_candidates[0]

    availability = structured.get("availability")
    if not availability:
        for word in ("disponível", "indisponível", "esgotado", "sem estoque"):
            if word in body_text.lower():
                availability = word
                break

    product.update({
        "title": _clean(title)[:180] or "Produto Natura",
        "price": price,
        "old_price": old_price,
        "discount": None,
        "image": image,
        "availability": availability,
        "details_verified": True,
    })

    # Mantém somente dados públicos; não tenta login, carrinho ou checkout.
    discount = re.search(r"(\d{1,2})\s*%\s*(?:off|de desconto)", body_text, re.I)
    if discount:
        product["discount"] = discount.group(1) + "%"

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

            # Verifica os detalhes diretamente nas páginas públicas dos produtos.
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
