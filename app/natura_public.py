"""Teste público da loja Natura, sem login e sem coleta automática."""
import re
from urllib.parse import urljoin
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

STORE_URL = "https://www.minhaloja.natura.com/consultoria/lucasuy"

async def collect_natura_public_once(max_products=10):
    products = []
    seen = set()
    errors = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = await browser.new_context(
            locale="pt-BR",
            timezone_id="America/Sao_Paulo",
            viewport={"width": 1365, "height": 900},
        )
        page = await context.new_page()
        try:
            await page.goto(STORE_URL, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(3500)
            # Load a little more of the public catalog.
            for _ in range(2):
                await page.mouse.wheel(0, 1800)
                await page.wait_for_timeout(1000)

            anchors = await page.locator("a").evaluate_all(
                """els => els.map(a => ({
                    href: a.href || a.getAttribute('href') || '',
                    text: (a.innerText || a.textContent || '').trim()
                }))"""
            )
            blocked = (
                "/consultoria/", "/carrinho", "/login", "/entrar",
                "/cadastro", "/favoritos", "/checkout", "/busca"
            )
            for item in anchors:
                href = str(item.get("href") or "").split("#")[0].split("?")[0]
                text = re.sub(r"\s+", " ", str(item.get("text") or "")).strip()
                if not href.startswith("https://www.minhaloja.natura.com/"):
                    continue
                if href.rstrip("/") == STORE_URL.rstrip("/"):
                    continue
                if any(part in href.lower() for part in blocked):
                    continue
                # Product links on the public catalog normally contain a
                # product/slug segment; reject generic navigation pages.
                path = href.split("minhaloja.natura.com/", 1)[-1]
                if not path or path.count("/") < 1:
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
