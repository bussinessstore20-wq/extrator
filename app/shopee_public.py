"""Descoberta pública de produtos Shopee sem API de afiliados e sem login.

A fonte é exclusivamente a página pública de busca/produto. Nenhum endpoint
privado, login, CAPTCHA ou mecanismo de acesso é contornado.
"""
import html
import re
import unicodedata
from urllib.parse import quote

import httpx

CATEGORIES = ["eletronicos", "casa", "beleza", "moda", "acessorios"]

BRAZIL = (
    "acre","alagoas","amapa","amazonas","bahia","ceara","distrito federal",
    "espirito santo","goias","maranhao","mato grosso","mato grosso do sul",
    "minas gerais","para","paraiba","parana","pernambuco","piaui",
    "rio de janeiro","rio grande do norte","rio grande do sul","rondonia",
    "roraima","santa catarina","sao paulo","sergipe","tocantins",
    "brasil","recife","fortaleza","salvador","belo horizonte","curitiba",
    "porto alegre","campinas","guarulhos",
)

FOREIGN = (
    "produto internacional objeto de declaracao de importacao",
    "sujeito a impostos estaduais e federais",
    "enviado de exterior",
    "shipping from exterior",
    "china","hong kong","coreia","japao","vietna","importado do",
)

def _norm(text):
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", text).strip().lower()

def classify(text):
    n = _norm(text)
    foreign = next((x for x in FOREIGN if _norm(x) in n), None)
    if foreign:
        return "international", foreign
    # We only accept a Brazilian location when it follows an explicit
    # shipping-origin label; generic mentions of Brazil are not enough.
    m = re.search(r"(?:enviado\s+de|shipping\s+from)\s*[:\-]?\s*([^|\n<]{2,80})", n)
    if m:
        origin = m.group(1).strip()
        if any(_norm(x) in origin for x in BRAZIL):
            return "national", origin
    return "unknown", None

def _product_links(page_html):
    decoded = html.unescape(page_html or "")
    # Search result pages expose product URLs in anchors and sometimes in
    # escaped JSON. Keep only public Shopee Brazil product paths.
    candidates = re.findall(
        r'(?:href=|\\?"url\\?":\\?")\\?["\']?(https?://(?:www\.)?shopee\.com\.br/[^"\'<>\\\s]+|/[^"\'<>\\\s]*-i\.\d+\.\d+[^"\'<>\\\s]*)',
        decoded,
        flags=re.IGNORECASE,
    )
    out, seen = [], set()
    for raw in candidates:
        url = raw
        if url.startswith("/"):
            url = "https://shopee.com.br" + url
        url = url.replace("\\/", "/").split("?")[0]
        if "-i." not in url or not url.startswith("https://shopee.com.br/"):
            continue
        if url in seen:
            continue
        seen.add(url)
        out.append(url)
    return out

def _title(page_html):
    m = re.search(r"<title[^>]*>(.*?)</title>", page_html or "", flags=re.I|re.S)
    return re.sub(r"\s+", " ", html.unescape(re.sub("<[^>]+>", " ", m.group(1)))).strip()[:180] if m else "Produto Shopee"

async def collect_public_once(max_products=5, pages_per_category=2):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0.0.0 Safari/537.36",
        "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
        "Accept": "text/html,application/xhtml+xml",
    }
    discovered, seen = [], set()
    national, international, unknown, errors = [], [], [], []
    pages_checked = 0

    async with httpx.AsyncClient(timeout=httpx.Timeout(20, connect=8), follow_redirects=True, headers=headers) as client:
        for category in CATEGORIES:
            if len(discovered) >= max_products * 10:
                break
            try:
                search_url = "https://shopee.com.br/search?keyword=" + quote(category)
                response = await client.get(search_url)
                if response.status_code >= 400:
                    errors.append({"category": category, "error": f"busca HTTP {response.status_code}"})
                    continue
                for url in _product_links(response.text):
                    if url not in seen:
                        seen.add(url)
                        discovered.append({"url": url, "title": ""})
                    if len(discovered) >= max_products * 10:
                        break
            except httpx.HTTPError as exc:
                errors.append({"category": category, "error": f"{type(exc).__name__}: {str(exc)[:160]}"})

        for item in discovered:
            if len(national) >= max_products:
                break
            try:
                response = await client.get(item["url"])
                pages_checked += 1
                if response.status_code >= 400:
                    errors.append({"url": item["url"], "error": f"produto HTTP {response.status_code}"})
                    continue
                body = response.text
                classification, evidence = classify(body)
                result = {
                    "title": _title(body),
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
            except httpx.HTTPError as exc:
                errors.append({"url": item["url"], "error": f"{type(exc).__name__}: {str(exc)[:160]}"})

    return {
        "source": "public_http",
        "categories_checked": len(CATEGORIES),
        "products_discovered": len(discovered),
        "product_pages_checked": pages_checked,
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
