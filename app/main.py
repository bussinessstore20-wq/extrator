import asyncio
import logging
import secrets
from contextlib import asynccontextmanager
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import RedirectResponse, HTMLResponse
from app import config
from app.db import get_db, log_event
from app.mercadolivre import collect_once
from telegram import Update
from app.telegram_bot import start_bot, send_pending_products, telegram_webhook_secret

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
# HTTP client URLs may contain Telegram bot tokens; never emit them in application logs.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger("extrator")
runtime = {"bot": None, "collector_task": None, "review_task": None, "webhook_task": None, "last_collection": None, "last_error": None}

async def collector_loop():
    while True:
        try:
            if config.COLLECTOR_ENABLED and config.supabase_ready() and config.ML_CLIENT_ID:
                result = await collect_once()
                runtime["last_collection"] = result
                category_errors = result.get("category_errors") or []
                runtime["last_error"] = (
                    str(category_errors[0].get("error") or "Falha na busca do Mercado Livre")[:500]
                    if category_errors else
                    ("A busca não retornou produtos; verifique o acesso à API de busca." if result.get("items_seen", 0) == 0 else None)
                )
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

async def webhook_loop():
    """Configura o webhook com novas tentativas sem bloquear a inicialização da API."""
    delay = 5
    while True:
        try:
            application = runtime.get("bot")
            if application and config.TELEGRAM_WEBHOOK_URL:
                await application.bot.set_webhook(
                    url=config.TELEGRAM_WEBHOOK_URL,
                    secret_token=telegram_webhook_secret(),
                    allowed_updates=Update.ALL_TYPES,
                    drop_pending_updates=False,
                )
                logger.info("Webhook do Telegram configurado com sucesso")
                return
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Não foi possível configurar o webhook do Telegram; nova tentativa em %s s", delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 300)

@asynccontextmanager
async def lifespan(app: FastAPI):
    if config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_ADMIN_IDS:
        try:
            runtime["bot"] = await start_bot()
        except Exception:
            logger.exception("Não foi possível iniciar o bot Telegram")
    if runtime["bot"]:
        runtime["webhook_task"] = asyncio.create_task(webhook_loop())
    runtime["collector_task"] = asyncio.create_task(collector_loop())
    runtime["review_task"] = asyncio.create_task(review_loop())
    yield
    for key in ("collector_task", "review_task", "webhook_task"):
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
            await bot.stop()
        except Exception:
            logger.exception("Erro ao parar o bot")
        try:
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


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    application = runtime.get("bot")
    if not application:
        raise HTTPException(status_code=503, detail="Bot Telegram ainda não está inicializado.")
    received_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not secrets.compare_digest(received_secret, telegram_webhook_secret()):
        raise HTTPException(status_code=403, detail="Não autorizado.")
    try:
        payload = await request.json()
        update = Update.de_json(payload, application.bot)
        await application.process_update(update)
    except Exception:
        logger.exception("Falha ao processar atualização do Telegram")
        raise HTTPException(status_code=500, detail="Falha ao processar atualização.")
    return {"ok": True}

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
    if not config.mercadolivre_ready():
        raise HTTPException(503, "Configure ML_CLIENT_ID, ML_CLIENT_SECRET e ML_REDIRECT_URI no Render.")
    if not config.supabase_ready():
        raise HTTPException(503, "Configure o Supabase antes de iniciar a autorização OAuth.")
    import base64
    import hashlib
    from datetime import datetime, timezone
    from urllib.parse import urlencode

    # PKCE S256: verifier and state are generated for each authorization attempt.
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    state = secrets.token_urlsafe(32)
    get_db().table("extrator_settings").upsert({
        "key": "ml_oauth_pkce",
        "value": {"state": state, "code_verifier": verifier, "created_at": datetime.now(timezone.utc).isoformat()},
    }).execute()

    params = urlencode({
        "response_type": "code",
        "client_id": config.ML_CLIENT_ID,
        "redirect_uri": config.ML_REDIRECT_URI,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    return RedirectResponse(f"https://auth.mercadolivre.com.br/authorization?{params}", status_code=302)

@app.get("/oauth/mercadolivre/callback")
async def oauth_callback(code: str = "", state: str = "", error: str = ""):
    if error:
        raise HTTPException(400, f"Autorização recusada pelo Mercado Livre: {error}")
    if not code or not state or not config.mercadolivre_ready():
        raise HTTPException(400, "Código OAuth ou configuração ausente.")
    if not config.supabase_ready():
        raise HTTPException(503, "Configure o Supabase antes de concluir a autorização OAuth.")

    from datetime import datetime, timezone, timedelta
    import httpx

    rows = get_db().table("extrator_settings").select("value").eq("key", "ml_oauth_pkce").limit(1).execute().data
    pending = (rows[0].get("value") or {}) if rows else {}
    expected_state = str(pending.get("state") or "")
    verifier = str(pending.get("code_verifier") or "")
    created_at = str(pending.get("created_at") or "")
    if not expected_state or not verifier or not secrets.compare_digest(state, expected_state):
        raise HTTPException(403, "Estado OAuth inválido ou expirado. Inicie novamente a conexão com o Mercado Livre.")
    try:
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        if datetime.now(timezone.utc) - created > timedelta(minutes=15):
            raise HTTPException(400, "A autorização expirou. Inicie novamente a conexão com o Mercado Livre.")
    except ValueError:
        raise HTTPException(400, "Não foi possível validar a tentativa OAuth. Inicie novamente a conexão.")

    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post("https://api.mercadolibre.com/oauth/token", data={
            "grant_type": "authorization_code",
            "client_id": config.ML_CLIENT_ID,
            "client_secret": config.ML_CLIENT_SECRET,
            "code": code,
            "redirect_uri": config.ML_REDIRECT_URI,
            "code_verifier": verifier,
        })
    if response.status_code >= 400:
        provider_error = {}
        try:
            payload = response.json()
            provider_error = {k: payload.get(k) for k in ("error", "message", "error_description") if payload.get(k)}
        except Exception:
            pass
        logger.error("Falha OAuth Mercado Livre HTTP %s; tipo=%s", response.status_code, provider_error.get("error", "não informado"))
        raise HTTPException(502, f"O Mercado Livre recusou a troca do código (HTTP {response.status_code}). Verifique a URI cadastrada e reinicie a autorização. Detalhe: {str(provider_error)[:250] or 'sem detalhe retornado'}.")

    data = response.json()
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=int(data.get("expires_in") or 0))).isoformat()
    get_db().table("extrator_settings").upsert({
        "key": "ml_oauth",
        "value": {"access_token": data.get("access_token", ""), "refresh_token": data.get("refresh_token", ""), "expires_at": expires_at}
    }).execute()
    get_db().table("extrator_settings").delete().eq("key", "ml_oauth_pkce").execute()
    log_event("mercadolivre_oauth_authorized", details={"user_id": data.get("user_id")})
    return {"status": "authorized", "message": "Mercado Livre conectado via OAuth com PKCE. Tokens armazenados no banco privado; nenhum token será exibido."}
@app.post("/admin/collector/run")
async def run_collection(x_admin_secret: str | None = Header(default=None)):
    if not config.ADMIN_API_SECRET or x_admin_secret != config.ADMIN_API_SECRET:
        raise HTTPException(403, "Não autorizado.")
    if not config.supabase_ready():
        raise HTTPException(503, "Supabase não configurado.")
    try:
        result = await collect_once()
        runtime["last_collection"] = result
        category_errors = result.get("category_errors") or []
        runtime["last_error"] = (
            str(category_errors[0].get("error") or "Falha na busca do Mercado Livre")[:500]
            if category_errors else
            ("A busca não retornou produtos; verifique o acesso à API de busca." if result.get("items_seen", 0) == 0 else None)
        )
        result_status = "partial" if category_errors else ("empty" if result.get("items_seen", 0) == 0 else "completed")
        return {"status": result_status, **result}
    except Exception as exc:
        runtime["last_error"] = f"{type(exc).__name__}: {exc}"[:500]
        logger.exception("Falha na coleta manual do Mercado Livre")
        raise HTTPException(502, f"Coleta bloqueada: {str(exc)[:450]}. Se o código for PA_UNAUTHORIZED_RESULT_FROM_POLICIES, habilite as permissões funcionais necessárias no DevCenter do Mercado Livre e confirme que o app está ativo.") from exc


@app.get('/admin/collector', response_class=HTMLResponse)
async def collector_dashboard():
    return HTMLResponse("<!doctype html><html lang=\"pt-BR\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>Extrator | Coleta</title><style>\nbody{margin:0;background:#0b1020;color:#edf2ff;font:16px system-ui,sans-serif}main{max-width:760px;margin:auto;padding:24px 16px}.card{background:#141c31;border:1px solid #2b3652;border-radius:16px;padding:22px;margin:16px 0}p{color:#aab5d0;line-height:1.5}input,button{width:100%;box-sizing:border-box;padding:14px;border-radius:10px;font-size:16px;margin-top:10px}input{background:#0b1224;border:1px solid #3a4767;color:white}button{border:0;background:#8068ff;color:white;font-weight:700;cursor:pointer}button:disabled{opacity:.6}.result{white-space:pre-wrap;background:#0b1224;padding:14px;border-radius:10px;margin-top:14px;min-height:44px}.stats{display:flex;gap:10px;flex-wrap:wrap}.stats div{background:#0b1224;padding:12px;border-radius:10px;flex:1;min-width:120px}small{color:#aab5d0}</style></head><body><main><h1>🦊 Extrator</h1><p>Central de coleta · Mercado Livre</p><section class=\"card\"><h2>Executar coleta manual</h2><p>Faça uma coleta sem ativar a rotina automática. Os novos produtos serão salvos como pendentes de aprovação e enviados ao Telegram quando o bot estiver conectado.</p><label for=\"secret\">Chave administrativa (ADMIN_API_SECRET)</label><input id=\"secret\" type=\"password\" autocomplete=\"current-password\" placeholder=\"Chave configurada no Render\"><button id=\"run\">▶ Executar coleta agora</button><div id=\"result\" class=\"result\" role=\"status\">Pronto para executar.</div></section><section class=\"card\"><h2>Status do serviço</h2><div class=\"stats\"><div><b id=\"db\">—</b><br><small>Supabase</small></div><div><b id=\"ml\">—</b><br><small>App Mercado Livre</small></div><div><b id=\"auto\">—</b><br><small>Coleta automática</small></div></div><button id=\"refresh\" style=\"background:#263451\">↻ Atualizar status</button><p><a href=\"/status\" style=\"color:#c8bcff\">Abrir status técnico</a></p></section></main><script>\nconst el=id=>document.getElementById(id);\nasync function refresh(){try{const r=await fetch('/status',{cache:'no-store'});const s=await r.json();el('db').textContent=s.supabase_connection==='ok'?'Conectado':s.supabase_connection;el('ml').textContent=s.mercadolivre_app_configured?'Configurado':'Não configurado';el('auto').textContent=s.collector_enabled?'Ligada':'Desligada'}catch(e){el('result').textContent='Não foi possível consultar o status.'}}\nel('refresh').addEventListener('click',refresh);\nel('run').addEventListener('click',async()=>{const secret=el('secret').value.trim();if(!secret){el('result').textContent='Informe ADMIN_API_SECRET, configurada nas variáveis do Render.';return}const b=el('run');b.disabled=true;b.textContent='Coletando…';el('result').textContent='Consultando o Mercado Livre. Aguarde…';try{const r=await fetch('/admin/collector/run',{method:'POST',headers:{'X-Admin-Secret':secret}});const d=await r.json();if(!r.ok){el('result').textContent=r.status===403?'Acesso negado. Confira a chave administrativa no Render.':(d.detail||'A coleta falhou.')}else{el('result').textContent='Coleta concluída!\\n\\nCategorias consultadas: '+(d.categories_seen||0)+'\\nProdutos encontrados: '+(d.items_seen||0)+'\\nNovos produtos salvos: '+(d.new_items||0)+'\\nErros: '+(d.errors||0)+'\\n\\nVerifique o Telegram para aprovar os produtos.';await refresh()}}catch(e){el('result').textContent='Falha de comunicação. Confira /status e os logs do Render.'}finally{b.disabled=false;b.textContent='▶ Executar coleta agora'}});\nrefresh();\n</script></body></html>")
