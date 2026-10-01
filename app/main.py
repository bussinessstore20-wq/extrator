import asyncio
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import RedirectResponse
from app import config
from app.db import get_db, log_event
from app.mercadolivre import collect_once
from app.telegram_bot import start_bot, send_pending_products

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("extrator")
runtime = {"bot": None, "collector_task": None, "review_task": None, "last_collection": None, "last_error": None}

async def collector_loop():
    while True:
        try:
            if config.COLLECTOR_ENABLED and config.supabase_ready() and config.ML_CLIENT_ID:
                runtime["last_collection"] = await collect_once()
                runtime["last_error"] = None
            await asyncio.sleep(config.COLLECTOR_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            runtime["last_error"] = str(exc)[:500]
            logger.exception("Falha no ciclo de coleta")
            await asyncio.sleep(min(config.COLLECTOR_INTERVAL_SECONDS, 300))

async def review_loop():
    while True:
        try:
            if runtime["bot"] and config.telegram_ready() and config.supabase_ready():
                await send_pending_products(runtime["bot"], limit=10)
            await asyncio.sleep(15)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Falha ao despachar fila de aprovação")
            await asyncio.sleep(15)

@asynccontextmanager
async def lifespan(app: FastAPI):
    if config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_ADMIN_IDS:
        try:
            runtime["bot"] = await start_bot()
        except Exception:
            logger.exception("Não foi possível iniciar o bot Telegram")
    runtime["collector_task"] = asyncio.create_task(collector_loop())
    runtime["review_task"] = asyncio.create_task(review_loop())
    yield
    for key in ("collector_task", "review_task"):
        task = runtime.get(key)
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    bot = runtime.get("bot")
    if bot:
        try:
            await bot.updater.stop()
            await bot.stop()
            await bot.shutdown()
        except Exception:
            logger.exception("Erro ao encerrar o bot")

app = FastAPI(title="Extrator Mercado Livre", version="0.1.0", lifespan=lifespan)

@app.get("/")
async def root():
    return {"app": "Extrator", "purpose": "Coleta automática de produtos do Mercado Livre com aprovação no Telegram", "status_url": "/status"}

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.get("/status")
async def status():
    # A presença das variáveis não garante que a chave funcione.
    # Faz uma consulta pequena e real ao Supabase, sem expor credenciais.
    supabase_connection = "not_configured"
    supabase_error = None
    if config.supabase_ready():
        try:
            get_db().table("extrator_settings").select("key").limit(1).execute()
            supabase_connection = "ok"
        except Exception as exc:
            supabase_connection = "error"
            supabase_error = type(exc).__name__
            logger.exception("Teste de conexão com Supabase falhou")
    return {
        "app": "extrator",
        "supabase_configured": config.supabase_ready(),
        "supabase_connection": supabase_connection,
        "supabase_error": supabase_error,
        "telegram_configured": config.telegram_ready(),
        "mercadolivre_app_configured": config.mercadolivre_ready(),
        "collector_enabled": config.COLLECTOR_ENABLED,
        "last_collection": runtime["last_collection"],
        "last_error": runtime["last_error"],
    }

@app.get("/oauth/mercadolivre/start")
async def oauth_start():
    if not config.mercadolivre_ready() or not config.ML_OAUTH_STATE:
        raise HTTPException(503, "Configure ML_CLIENT_ID, ML_CLIENT_SECRET, ML_REDIRECT_URI e ML_OAUTH_STATE no Render.")
    from urllib.parse import urlencode
    params = urlencode({
        "response_type": "code",
        "client_id": config.ML_CLIENT_ID,
        "redirect_uri": config.ML_REDIRECT_URI,
        "state": config.ML_OAUTH_STATE,
    })
    return RedirectResponse(f"https://auth.mercadolivre.com.br/authorization?{params}", status_code=302)

@app.get("/oauth/mercadolivre/callback")
async def oauth_callback(code: str = "", state: str = "", error: str = ""):
    if error:
        raise HTTPException(400, f"Autorização recusada pelo Mercado Livre: {error}")
    if not code or not config.mercadolivre_ready() or not config.ML_OAUTH_STATE:
        raise HTTPException(400, "Código OAuth ou configuração ausente.")
    if state != config.ML_OAUTH_STATE:
        raise HTTPException(403, "Parâmetro state inválido.")
    import httpx
    from datetime import datetime, timezone, timedelta
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post("https://api.mercadolibre.com/oauth/token", data={
            "grant_type": "authorization_code",
            "client_id": config.ML_CLIENT_ID,
            "client_secret": config.ML_CLIENT_SECRET,
            "code": code,
            "redirect_uri": config.ML_REDIRECT_URI,
        })
    if response.status_code >= 400:
        logger.error("Falha OAuth Mercado Livre HTTP %s", response.status_code)
        raise HTTPException(502, "O Mercado Livre não concluiu a troca do código. Confira redirect URI e credenciais nos logs do provedor.")
    data = response.json()
    if not config.supabase_ready():
        raise HTTPException(503, "Configure o Supabase antes de autorizar o Mercado Livre.")
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=int(data.get("expires_in") or 0))).isoformat()
    get_db().table("extrator_settings").upsert({
        "key": "ml_oauth",
        "value": {"access_token": data.get("access_token", ""), "refresh_token": data.get("refresh_token", ""), "expires_at": expires_at}
    }).execute()
    log_event("mercadolivre_oauth_authorized", details={"user_id": data.get("user_id")})
    return {"status": "authorized", "message": "Mercado Livre conectado. Tokens armazenados no banco privado; nenhum token será exibido."}

@app.post("/admin/collector/run")
async def run_collection(x_admin_secret: str | None = Header(default=None)):
    if not config.ADMIN_API_SECRET or x_admin_secret != config.ADMIN_API_SECRET:
        raise HTTPException(403, "Não autorizado.")
    if not config.supabase_ready():
        raise HTTPException(503, "Supabase não configurado.")
    try:
        result = await collect_once()
        runtime["last_collection"] = result
        runtime["last_error"] = None
        return {"status": "completed", **result}
    except Exception as exc:
        runtime["last_error"] = str(exc)[:500]
        raise HTTPException(502, "A coleta falhou; consulte os logs do Render.") from exc
