"""Teste público da loja Natura, sem login e sem coleta automática."""
import asyncio
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
                text = re.sub(r"\s+", " ", str(item.get("text") or "")).strip()
                card_text = re.sub(r"\s+", " ", str(item.get("cardText") or "")).strip()
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

                # Só considera candidatos que tenham evidência de produto.
                has_price = bool(re.search(r"r\$\s*[0-9][0-9.,]*", card_text, re.I))
                product_hint = bool(re.search(r"/(produto|produtos|item|itens)(/|$)", "/" + path, re.I))
                deep_store_path = path.startswith("consultoria/lucasuy/") and path.count("/") >= 2
                if not (has_price or product_hint or deep_store_path):
                    continue

                if href in seen:
                    continue
                seen.add(href)

                title = text
                if len(title) < 3 and card_text:
                    title = re.split(r"r\$\s*[0-9]", card_text, maxsplit=1, flags=re.I)[0].strip()

                products.append({
                    "title": title[:180] or "Produto Natura",
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
