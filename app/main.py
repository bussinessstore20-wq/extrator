import asyncio
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import RedirectResponse, HTMLResponse
from app import config
from app.db import get_db, log_event
from app.mercadolivre import collect_once
from app.telegram_bot import start_bot, send_pending_products

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
# HTTP client URLs may contain Telegram bot tokens; never emit them in application logs.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
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


@app.get('/admin/collector', response_class=HTMLResponse)
async def collector_dashboard():
    return HTMLResponse("<!doctype html><html lang=\"pt-BR\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>Extrator | Coleta</title><style>\nbody{margin:0;background:#0b1020;color:#edf2ff;font:16px system-ui,sans-serif}main{max-width:760px;margin:auto;padding:24px 16px}.card{background:#141c31;border:1px solid #2b3652;border-radius:16px;padding:22px;margin:16px 0}p{color:#aab5d0;line-height:1.5}input,button{width:100%;box-sizing:border-box;padding:14px;border-radius:10px;font-size:16px;margin-top:10px}input{background:#0b1224;border:1px solid #3a4767;color:white}button{border:0;background:#8068ff;color:white;font-weight:700;cursor:pointer}button:disabled{opacity:.6}.result{white-space:pre-wrap;background:#0b1224;padding:14px;border-radius:10px;margin-top:14px;min-height:44px}.stats{display:flex;gap:10px;flex-wrap:wrap}.stats div{background:#0b1224;padding:12px;border-radius:10px;flex:1;min-width:120px}small{color:#aab5d0}</style></head><body><main><h1>🦊 Extrator</h1><p>Central de coleta · Mercado Livre</p><section class=\"card\"><h2>Executar coleta manual</h2><p>Faça uma coleta sem ativar a rotina automática. Os novos produtos serão salvos como pendentes de aprovação e enviados ao Telegram quando o bot estiver conectado.</p><label for=\"secret\">Chave administrativa (ADMIN_API_SECRET)</label><input id=\"secret\" type=\"password\" autocomplete=\"current-password\" placeholder=\"Chave configurada no Render\"><button id=\"run\">▶ Executar coleta agora</button><div id=\"result\" class=\"result\" role=\"status\">Pronto para executar.</div></section><section class=\"card\"><h2>Status do serviço</h2><div class=\"stats\"><div><b id=\"db\">—</b><br><small>Supabase</small></div><div><b id=\"ml\">—</b><br><small>App Mercado Livre</small></div><div><b id=\"auto\">—</b><br><small>Coleta automática</small></div></div><button id=\"refresh\" style=\"background:#263451\">↻ Atualizar status</button><p><a href=\"/status\" style=\"color:#c8bcff\">Abrir status técnico</a></p></section></main><script>\nconst el=id=>document.getElementById(id);\nasync function refresh(){try{const r=await fetch('/status',{cache:'no-store'});const s=await r.json();el('db').textContent=s.supabase_connection==='ok'?'Conectado':s.supabase_connection;el('ml').textContent=s.mercadolivre_app_configured?'Configurado':'Não configurado';el('auto').textContent=s.collector_enabled?'Ligada':'Desligada'}catch(e){el('result').textContent='Não foi possível consultar o status.'}}\nel('refresh').addEventListener('click',refresh);\nel('run').addEventListener('click',async()=>{const secret=el('secret').value.trim();if(!secret){el('result').textContent='Informe ADMIN_API_SECRET, configurada nas variáveis do Render.';return}const b=el('run');b.disabled=true;b.textContent='Coletando…';el('result').textContent='Consultando o Mercado Livre. Aguarde…';try{const r=await fetch('/admin/collector/run',{method:'POST',headers:{'X-Admin-Secret':secret}});const d=await r.json();if(!r.ok){el('result').textContent=r.status===403?'Acesso negado. Confira a chave administrativa no Render.':(d.detail||'A coleta falhou.')}else{el('result').textContent='Coleta concluída!\\n\\nCategorias consultadas: '+(d.categories_seen||0)+'\\nProdutos encontrados: '+(d.items_seen||0)+'\\nNovos produtos salvos: '+(d.new_items||0)+'\\nErros: '+(d.errors||0)+'\\n\\nVerifique o Telegram para aprovar os produtos.';await refresh()}}catch(e){el('result').textContent='Falha de comunicação. Confira /status e os logs do Render.'}finally{b.disabled=false;b.textContent='▶ Executar coleta agora'}});\nrefresh();\n</script></body></html>")
