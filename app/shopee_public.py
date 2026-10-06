"""Coleta pública da Shopee via navegador, sem usar a API de afiliados.

Usa páginas públicas e apenas sinais visíveis no produto. Não faz login nem tenta
contornar CAPTCHA, autenticação ou controles de acesso.
"""
import asyncio
import re
from urllib.parse import quote, urlparse

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

CATEGORIES = [
    "eletronicos",
    "casa",
    "beleza",
    "moda",
    "acessorios",
]

BRAZIL = (
    "acre","alagoas","amapa","amazonas","bahia","ceara","distrito federal",
    "espirito santo","goias","maranhao","mato grosso","mato grosso do sul",
    "minas gerais","para","paraiba","parana","pernambuco","piaui",
    "rio de janeiro","rio grande do norte","rio grande do sul","rondonia",
    "roraima","santa catarina","sao paulo","sergipe","tocantins",
    "brasil","são paulo","rio de janeiro","recife","fortaleza","salvador",
    "belo horizonte","curitiba","porto alegre","campinas","guarulhos",
)

FOREIGN = (
    "produto internacional objeto de declaração de importação",
    "sujeito a impostos estaduais e federais",
    "enviado de exterior",
    "shipping from exterior",
    "china","hong kong","coreia","japão","japao","vietnã","vietna",
    "importado do",
)

def _norm(text):
    import unicodedata
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", text).strip().lower()

def classify(text):
    n = _norm(text)
    foreign_hit = next((x for x in FOREIGN if _norm(x) in n), None)
    if foreign_hit:
        return "international", foreign_hit
    # Prefer the explicit dispatch label over generic mentions of Brazil.
    m = re.search(r"(?:enviado de|shipping from)\s*[:\-]?\s*([^|\n]{2,80})", n)
    if m:
        origin = m.group(1).strip()
        if any(_norm(x) in origin for x in BRAZIL):
            return "national", origin
    return "unknown", None

async def _text(page):
    try:
        return await page.locator("body").inner_text(timeout=6000)
    except Exception:
        return ""

async def _collect_links(page, keyword, limit=20):
    url = "https://shopee.com.br/search?keyword=" + quote(keyword)
    await page.goto(url, wait_until="domcontentloaded", timeout=25000)
    await page.wait_for_timeout(2500)
    links = await page.locator('a[href*="-i."]').evaluate_all(
        """els => els.map(a => ({href:a.href, text:(a.innerText||a.textContent||"").trim()}))"""
    )
    out = []
    seen = set()
    for item in links:
        href = str(item.get("href") or "").split("?")[0]
        if not href.startswith("https://shopee.com.br/") or "-i." not in href:
            continue
        if href in seen:
            continue
        seen.add(href)
        out.append({"url": href, "title": str(item.get("text") or "").strip()[:180]})
        if len(out) >= limit:
            break
    return out

async def collect_public_once(max_products=5, pages_per_category=2):
    """Discover public product URLs and classify visible shipping evidence.

    The test intentionally does not save or publish anything. Automatic collection
    remains controlled by the existing application flag.
    """
    categories_checked = 0
    discovered = []
    page_checks = 0
    national = []
    international = []
    unknown = []
    errors = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            locale="pt-BR",
            timezone_id="America/Sao_Paulo",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()
        try:
            for category in CATEGORIES:
                if len(discovered) >= max_products * 8:
                    break
                categories_checked += 1
                try:
                    items = await _collect_links(page, category, limit=20)
                    for item in items:
                        if item["url"] not in {x["url"] for x in discovered}:
                            discovered.append(item)
                except PlaywrightTimeoutError:
                    errors.append({"category": category, "error": "tempo esgotado ao abrir busca pública"})
                except Exception as exc:
                    errors.append({"category": category, "error": f"{type(exc).__name__}: {str(exc)[:180]}"})

            # Inspect a bounded sample so the manual test stays controlled.
            for item in discovered[: max_products * 8]:
                if len(national) >= max_products:
                    break
                try:
                    await page.goto(item["url"], wait_until="domcontentloaded", timeout=20000)
                    await page.wait_for_timeout(1200)
                    body = await _text(page)
                    page_checks += 1
                    classification, evidence = classify(body)
                    result = {
                        "title": item["title"] or "Produto Shopee",
                        "permalink": item["url"],
                        "classification": classification,
                        "evidence": evidence,
                    }
                    if classification == "national":
                        national.append(result)
                    elif classification == "international":
                        international.append(result)
                    else:
                        unknown.append(result)
                except PlaywrightTimeoutError:
                    errors.append({"url": item["url"], "error": "tempo esgotado ao abrir produto"})
                except Exception as exc:
                    errors.append({"url": item["url"], "error": f"{type(exc).__name__}: {str(exc)[:180]}"})
        finally:
            await context.close()
            await browser.close()

    return {
        "source": "public_browser",
        "categories_checked": categories_checked,
        "products_discovered": len(discovered),
        "product_pages_checked": page_checks,
        "national_confirmed": len(national),
        "international_confirmed": len(international),
        "unknown": len(unknown),
        "national_links": [x["permalink"] for x in national],
        "international_links": [x["permalink"] for x in international[:10]],
        "unknown_links": [x["permalink"] for x in unknown[:10]],
        "national_samples": national,
        "international_samples": international[:10],
        "unknown_samples": unknown[:10],
        "errors": errors,
        "automatic_collection_enabled": False,
    }
