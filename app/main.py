import asyncio
import logging
import secrets
from contextlib import asynccontextmanager
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import RedirectResponse, HTMLResponse
from app import config
from app.db import get_db, log_event
from app.mercadolivre import collect_once
from app.shopee import collect_once as collect_shopee_once, shopee_ready
from telegram import Update
from app.telegram_bot import start_bot, send_pending_products, telegram_webhook_secret

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
# HTTP client URLs may contain Telegram bot tokens; never emit them in application logs.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger("extrator")
runtime = {"bot": None, "collector_task": None, "shopee_task": None, "review_task": None, "webhook_task": None, "last_collection": None, "last_error": None, "shopee_last_collection": None, "shopee_last_error": None, "shopee_collector_paused": False}

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

async def shopee_collector_loop():
    """Rotina Shopee separada; permanece desligada até SHOPEE_COLLECTOR_ENABLED=true."""
    while True:
        try:
            if config.SHOPEE_COLLECTOR_ENABLED and not runtime["shopee_collector_paused"] and config.supabase_ready() and shopee_ready():
                result = await collect_shopee_once()
                runtime["shopee_last_collection"] = result
                category_errors = result.get("category_errors") or []
                runtime["shopee_last_error"] = (
                    str(category_errors[0].get("error") or "Falha na busca Shopee")[:500]
                    if category_errors else
                    (result.get("warning") or ("Nenhum produto com envio nacional confirmado." if result.get("accepted_national", 0) == 0 else None))
                )
            await asyncio.sleep(config.SHOPEE_COLLECTOR_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            runtime["shopee_last_error"] = str(exc)[:500]
            logger.exception("Falha no ciclo de coleta Shopee")
            await asyncio.sleep(min(config.SHOPEE_COLLECTOR_INTERVAL_SECONDS, 300))

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
    runtime["shopee_task"] = asyncio.create_task(shopee_collector_loop())
    runtime["review_task"] = asyncio.create_task(review_loop())
    yield
    for key in ("collector_task", "shopee_task", "review_task", "webhook_task"):
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
        "shopee_app_configured": shopee_ready(),
        "shopee_collector_enabled": config.SHOPEE_COLLECTOR_ENABLED,
        "shopee_collector_interval_seconds": config.SHOPEE_COLLECTOR_INTERVAL_SECONDS,
        "shopee_max_items_per_cycle": config.SHOPEE_MAX_ITEMS_PER_CYCLE,
        "shopee_collector_paused": runtime["shopee_collector_paused"],
        "shopee_last_collection": runtime["shopee_last_collection"],
        "shopee_last_error": runtime["shopee_last_error"],
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
@app.post("/admin/shopee/collector/pause")
async def pause_shopee_collector(x_admin_secret: str | None = Header(default=None)):
    if not config.ADMIN_API_SECRET or not secrets.compare_digest(x_admin_secret or "", config.ADMIN_API_SECRET):
        raise HTTPException(403, "Não autorizado.")
    runtime["shopee_collector_paused"] = True
    return {"status": "paused", "message": "Coleta automática da Shopee pausada. Uma coleta já em andamento poderá terminar."}


@app.post("/admin/shopee/collector/resume")
async def resume_shopee_collector(x_admin_secret: str | None = Header(default=None)):
    if not config.ADMIN_API_SECRET or not secrets.compare_digest(x_admin_secret or "", config.ADMIN_API_SECRET):
        raise HTTPException(403, "Não autorizado.")
    runtime["shopee_collector_paused"] = False
    return {"status": "resumed", "message": "Coleta automática da Shopee retomada conforme SHOPEE_COLLECTOR_ENABLED."}


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


@app.post("/admin/shopee/collector/run")
async def run_shopee_collection(x_admin_secret: str | None = Header(default=None)):
    if not config.ADMIN_API_SECRET or not secrets.compare_digest(x_admin_secret or "", config.ADMIN_API_SECRET):
        raise HTTPException(403, "Não autorizado.")
    if not config.supabase_ready():
        raise HTTPException(503, "Supabase não configurado.")
    if not shopee_ready():
        raise HTTPException(503, "Configure SHOPEE_AFFILIATE_APP_ID e SHOPEE_AFFILIATE_SECRET nas variáveis do Render.")
    try:
        result = await collect_shopee_once()
        runtime["shopee_last_collection"] = result
        category_errors = result.get("category_errors") or []
        runtime["shopee_last_error"] = (
            str(category_errors[0].get("error") or "Falha na busca Shopee")[:500]
            if category_errors else
            (result.get("warning") or ("Nenhum produto com envio nacional confirmado." if result.get("accepted_national", 0) == 0 else None))
        )
        result_status = "partial" if category_errors else ("empty" if result.get("accepted_national", 0) == 0 else "completed")
        return {"status": result_status, **result, "automatic_collection_enabled": config.SHOPEE_COLLECTOR_ENABLED}
    except Exception as exc:
        runtime["shopee_last_error"] = f"{type(exc).__name__}: {exc}"[:500]
        logger.exception("Falha na coleta manual da Shopee")
        raise HTTPException(502, f"Coleta Shopee falhou: {str(exc)[:450]}") from exc


@app.get('/admin/collector', response_class=HTMLResponse)
async def collector_dashboard():
    return HTMLResponse(r"""<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#101827">
<title>Extrator | Central de operações</title>
<style>
:root{color-scheme:dark;--bg:#0b1120;--panel:#131d2f;--panel2:#0e1728;--line:#26364d;--text:#eef4ff;--muted:#9aaec8;--purple:#8b7cff;--green:#38c793;--amber:#f3b84b;--red:#fb7185}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(ellipse at top left,#172744 0,transparent 44%),var(--bg);color:var(--text);font:15px/1.5 Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
main{width:min(1080px,100%);margin:auto;padding:28px 18px 52px}.topbar{display:flex;align-items:center;justify-content:space-between;gap:18px;margin-bottom:26px}.brand{display:flex;align-items:center;gap:14px}.logo{display:grid;place-items:center;width:50px;height:50px;border-radius:16px;background:linear-gradient(135deg,#8b7cff,#5d52cf);font-size:25px;box-shadow:0 8px 30px #6558d633}.eyebrow{margin:0;color:#a8a0ff;font-size:11px;font-weight:800;letter-spacing:.16em;text-transform:uppercase}.brand h1{font-size:25px;letter-spacing:-.04em;margin:2px 0 0}.top-actions{display:flex;gap:10px;align-items:center}.top-actions a{color:#c7d2fe;text-decoration:none;font-size:13px}.layout{display:grid;grid-template-columns:1fr 1fr;gap:16px}.wide{grid-column:1/-1}.card{min-width:0;background:linear-gradient(180deg,#151f32,#111a2b);border:1px solid var(--line);border-radius:19px;padding:22px;box-shadow:0 12px 40px #00000012}.card-head{display:flex;justify-content:space-between;align-items:flex-start;gap:12px;margin-bottom:10px}.card h2{font-size:18px;letter-spacing:-.02em;margin:0 0 5px}.card h3{font-size:14px;margin:0}.sub{margin:0;color:var(--muted);font-size:13px;line-height:1.65}.pill{display:inline-flex;align-items:center;gap:7px;padding:6px 10px;border:1px solid var(--line);border-radius:999px;background:#0d1626;color:#b8c8df;font-size:11px;font-weight:750;white-space:nowrap}.dot{width:7px;height:7px;border-radius:50%;background:#8393aa}.pill.good .dot{background:var(--green)}.pill.warn .dot{background:var(--amber)}.pill.bad .dot{background:var(--red)}.metrics{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;margin-top:18px}.metric{background:var(--panel2);border:1px solid #223149;border-radius:13px;padding:13px;min-width:0}.metric b{display:block;font-size:17px;letter-spacing:-.02em;overflow-wrap:anywhere}.metric span{display:block;color:var(--muted);font-size:11px;margin-top:3px}.operations{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:18px}.operation{border:1px solid #2a3a53;border-radius:15px;padding:16px;background:#0e1728}.op-title{display:flex;align-items:center;gap:10px;margin-bottom:7px}.op-icon{display:grid;place-items:center;width:36px;height:36px;border-radius:11px;background:#202c44;font-size:18px}.op-title h3{font-size:14px}.op-title small{display:block;color:var(--muted);font-size:11px;font-weight:500}.field-label{display:block;color:#c6d3e7;font-size:12px;font-weight:700;margin-top:18px}input{width:100%;margin-top:7px;padding:13px 14px;border:1px solid #34445f;border-radius:11px;background:#091221;color:#fff;font:inherit;outline:none}input:focus{border-color:var(--purple);box-shadow:0 0 0 3px #8b7cff22}.btn{display:inline-flex;justify-content:center;align-items:center;gap:8px;border:0;border-radius:11px;padding:12px 14px;font:inherit;font-weight:800;font-size:13px;cursor:pointer;transition:filter .15s,transform .15s;width:100%;margin-top:10px}.btn:hover{filter:brightness(1.08)}.btn:active{transform:translateY(1px)}.btn:disabled{opacity:.55;cursor:wait}.primary{background:var(--purple);color:white}.secondary{background:#263650;color:#e3edff}.orange{background:#8b5a15;color:#fff0d0}.green{background:#146b51;color:#d9fff1}.notice{margin-top:15px;padding:13px 14px;border-radius:12px;border:1px solid #6d552b;background:#382b16;color:#f6dca4;font-size:12px;line-height:1.6}.notice strong{display:block;margin-bottom:3px}.notice.info{border-color:#314c72;background:#14253d;color:#c8dcfa}.result{white-space:pre-wrap;overflow-wrap:anywhere;min-height:46px;margin-top:13px;padding:13px;border:1px solid #253650;border-radius:12px;background:#091221;color:#c7d6eb;font-size:12px;line-height:1.7}.result:empty{display:none}.divider{height:1px;background:#25344a;margin:18px 0}.section-label{color:#8399b7;font-size:10px;font-weight:800;letter-spacing:.15em;text-transform:uppercase;margin:0 0 10px}.last-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}.last-box{background:#0d1626;border:1px solid #223149;border-radius:13px;padding:14px;min-width:0}.last-box p{color:var(--muted);font-size:12px;margin:6px 0 0;overflow-wrap:anywhere}.footer{display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;color:#7287a5;font-size:11px;padding:18px 3px}.footer a{color:#a9a0ff;text-decoration:none}
@media(max-width:700px){main{padding:18px 12px 36px}.topbar{align-items:flex-start}.brand h1{font-size:21px}.top-actions{flex-direction:column;align-items:flex-end}.layout,.operations{grid-template-columns:1fr}.wide{grid-column:auto}.metrics{grid-template-columns:repeat(2,minmax(0,1fr))}.card{padding:17px}.last-grid{grid-template-columns:1fr}}
</style>
</head>
<body>
<main>
<header class="topbar">
  <div class="brand"><div class="logo">🦊</div><div><p class="eyebrow">Central de operações</p><h1>Extrator</h1></div></div>
  <div class="top-actions"><span id="overall" class="pill"><i class="dot"></i>Verificando serviço</span><a href="/status" target="_blank" rel="noopener">Status técnico ↗</a></div>
</header>
<div class="layout">
  <section class="card wide">
    <div class="card-head"><div><h2>Visão geral</h2><p class="sub">Acompanhe as conexões e execute testes manuais sem ligar as rotinas automáticas.</p></div><span id="dbPill" class="pill"><i class="dot"></i>Banco</span></div>
    <div class="metrics">
      <div class="metric"><b id="db">—</b><span>Conexão Supabase</span></div>
      <div class="metric"><b id="ml">—</b><span>Credenciais Mercado Livre</span></div>
      <div class="metric"><b id="shopee">—</b><span>Credenciais Shopee</span></div>
      <div class="metric"><b id="updated">—</b><span>Última atualização</span></div>
    </div>
    <div class="divider"></div>
    <p class="section-label">Rotinas automáticas</p>
    <div class="metrics">
      <div class="metric"><b id="auto">—</b><span>Mercado Livre</span></div>
      <div class="metric"><b id="shopeeAuto">—</b><span>Shopee</span></div>
      <div class="metric"><b id="pauseState">—</b><span>Controle Shopee</span></div>
      <div class="metric"><b id="interval">10 min</b><span>Intervalo Shopee</span></div>
    </div>
  </section>
  <section class="card wide">
    <div class="card-head"><div><h2>Chave administrativa</h2><p class="sub">A chave é enviada ao servidor apenas ao executar uma ação. Ela não é salva pelo painel.</p></div><span class="pill"><i class="dot"></i>Acesso protegido</span></div>
    <label class="field-label" for="secret">ADMIN_API_SECRET — configurada no Render</label>
    <input id="secret" type="password" autocomplete="current-password" placeholder="Digite sua chave administrativa">
  </section>
  <section class="card">
    <div class="card-head"><div><h2>Mercado Livre</h2><p class="sub">Executa uma busca manual usando a configuração existente. Não altera a rotina automática.</p></div><span class="pill"><i class="dot"></i>Manual</span></div>
    <div class="operation" style="margin-top:16px">
      <div class="op-title"><div class="op-icon">🛒</div><div><h3>Executar coleta</h3><small>Mercado Livre · configuração atual</small></div></div>
      <p class="sub">Os produtos novos seguem para aprovação conforme o fluxo já configurado.</p>
      <button id="run" class="btn primary">▶ Executar coleta Mercado Livre</button>
    </div>
    <div id="mlResult" class="result" role="status">Aguardando execução manual.</div>
  </section>
  <section class="card">
    <div class="card-head"><div><h2>Shopee</h2><p class="sub">Teste independente com limite de 5 links novos por ciclo, em intervalos de 10 minutos.</p></div><span id="shopeePill" class="pill warn"><i class="dot"></i>Origem não confirmada</span></div>
    <div class="operation" style="margin-top:16px">
      <div class="op-title"><div class="op-icon">🛍️</div><div><h3>Testar coleta Shopee</h3><small>API oficial de afiliados</small></div></div>
      <p class="sub">A coleta automática permanece desligada. O teste manual não a ativa.</p>
      <button id="runShopee" class="btn primary">▶ Testar coleta Shopee</button>
      <button id="pauseButton" class="btn orange">⏸ Pausar Shopee</button>
    </div>
    <div class="notice"><strong>Filtro de origem nacional ativo</strong>Produtos sem informação confiável de envio nacional são excluídos. Não serão enviados links de origem desconhecida.</div>
    <div id="shopeeResult" class="result" role="status">Aguardando teste manual.</div>
  </section>
  <section class="card wide">
    <div class="card-head"><div><h2>Últimos resultados</h2><p class="sub">Resumo da última execução disponível nesta instância do serviço.</p></div><button id="refresh" class="btn secondary" style="width:auto;margin:0">↻ Atualizar status</button></div>
    <div class="last-grid">
      <div class="last-box"><h3>Mercado Livre</h3><p id="lastMl">Nenhuma execução registrada nesta instância.</p></div>
      <div class="last-box"><h3>Shopee</h3><p id="lastShopee">Nenhuma execução registrada nesta instância.</p></div>
    </div>
    <div id="result" class="result" role="status"></div>
  </section>
</div>
<footer class="footer"><span>Extrator · Painel operacional</span><span>As coletas manuais não ativam tarefas automáticas.</span></footer>
</main>
<script>
const el = id => document.getElementById(id);
let statusData = null;
function setPill(id, text, kind) {
  const node = el(id); node.className = 'pill' + (kind ? ' ' + kind : ''); node.innerHTML = '<i class="dot"></i>' + text;
}
function describeRun(run, platform) {
  if (!run) return 'Nenhuma execução registrada nesta instância.';
  const parts = [];
  parts.push('Produtos encontrados: ' + (run.items_seen ?? 0));
  if (platform === 'shopee') {
    parts.push('Origem nacional confirmada: ' + (run.accepted_national ?? 0));
    parts.push('Origem desconhecida excluída: ' + (run.excluded_unknown_origin ?? 0));
    parts.push('Links novos salvos: ' + (run.new_items ?? 0));
  } else parts.push('Novos produtos salvos: ' + (run.new_items ?? 0));
  parts.push('Erros: ' + (run.errors ?? 0));
  return parts.join(' · ');
}
async function refresh() {
  try {
    const r = await fetch('/status', {cache:'no-store'});
    const s = await r.json();
    statusData = s;
    el('db').textContent = s.supabase_connection === 'ok' ? 'Conectado' : (s.supabase_connection === 'error' ? 'Erro' : 'Não configurado');
    el('ml').textContent = s.mercadolivre_app_configured ? 'Configurado' : 'Pendente';
    el('shopee').textContent = s.shopee_app_configured ? 'Configurado' : 'Pendente';
    el('auto').textContent = s.collector_enabled ? 'Ligada' : 'Desligada';
    el('shopeeAuto').textContent = s.shopee_collector_enabled ? 'Ligada' : 'Desligada';
    el('pauseState').textContent = s.shopee_collector_paused ? 'Pausada' : (s.shopee_collector_enabled ? 'Ativa' : 'Desligada');
    el('interval').textContent = Math.round((s.shopee_collector_interval_seconds || 600) / 60) + ' min';
    el('updated').textContent = new Date().toLocaleTimeString('pt-BR', {hour:'2-digit',minute:'2-digit'});
    setPill('overall', s.supabase_connection === 'ok' ? 'Serviço conectado' : 'Verificar conexão', s.supabase_connection === 'ok' ? 'good' : 'warn');
    setPill('dbPill', s.supabase_connection === 'ok' ? 'Banco conectado' : 'Banco: ' + s.supabase_connection, s.supabase_connection === 'ok' ? 'good' : 'warn');
    setPill('shopeePill', s.shopee_last_collection && s.shopee_last_collection.accepted_national > 0 ? 'Nacionais confirmados' : 'Origem não confirmada', s.shopee_last_collection && s.shopee_last_collection.accepted_national > 0 ? 'good' : 'warn');
    el('pauseButton').textContent = s.shopee_collector_paused ? '▶ Retomar Shopee' : '⏸ Pausar Shopee';
    el('pauseButton').className = 'btn ' + (s.shopee_collector_paused ? 'green' : 'orange');
    el('lastMl').textContent = describeRun(s.last_collection, 'ml') + (s.last_error ? ' · Aviso: ' + s.last_error : '');
    el('lastShopee').textContent = describeRun(s.shopee_last_collection, 'shopee') + (s.shopee_last_error ? ' · Aviso: ' + s.shopee_last_error : '');
  } catch (e) {
    setPill('overall', 'Falha ao consultar', 'bad');
    el('result').textContent = 'Não foi possível consultar /status. Confira o serviço e tente novamente.';
  }
}
async function postAction(url, buttonId, resultId, loadingText, render) {
  const secret = el('secret').value.trim();
  if (!secret) { el(resultId).textContent = 'Informe ADMIN_API_SECRET antes de executar esta ação.'; el('secret').focus(); return; }
  const button = el(buttonId); const oldText = button.textContent;
  button.disabled = true; button.textContent = loadingText;
  el(resultId).textContent = 'Solicitação em andamento. Aguarde…';
  try {
    const r = await fetch(url, {method:'POST',headers:{'X-Admin-Secret':secret}});
    const d = await r.json();
    if (!r.ok) { el(resultId).textContent = d.detail || 'A operação não foi concluída.'; return; }
    render(d);
    await refresh();
  } catch (e) {
    el(resultId).textContent = 'Falha de comunicação. Atualize o status e confira os logs do serviço.';
  } finally {
    button.disabled = false; button.textContent = oldText; await refresh();
  }
}
el('refresh').addEventListener('click', refresh);
el('pauseButton').addEventListener('click', async () => {
  const paused = statusData && statusData.shopee_collector_paused;
  const resume = paused;
  await postAction(resume ? '/admin/shopee/collector/resume' : '/admin/shopee/collector/pause', 'pauseButton', 'result', resume ? 'Retomando…' : 'Pausando…', d => { el('result').textContent = d.message || 'Estado da coleta Shopee atualizado.'; });
});
el('run').addEventListener('click', async () => {
  await postAction('/admin/collector/run', 'run', 'mlResult', 'Coletando…', d => {
    el('mlResult').textContent = 'Coleta Mercado Livre concluída.\nCategorias: ' + (d.categories_seen || 0) + '\nProdutos encontrados: ' + (d.items_seen || 0) + '\nNovos produtos: ' + (d.new_items || 0) + '\nErros: ' + (d.errors || 0);
  });
});
el('runShopee').addEventListener('click', async () => {
  await postAction('/admin/shopee/collector/run', 'runShopee', 'shopeeResult', 'Consultando Shopee…', d => {
    el('shopeeResult').textContent = 'Teste Shopee concluído.\nCategorias consultadas: ' + (d.categories_seen || 0) + '\nProdutos encontrados: ' + (d.items_seen || 0) + '\nPáginas consultadas para origem: ' + (d.product_pages_checked || 0) + '\nAnúncios com campo Enviado de encontrado: ' + (d.origin_values_found || 0) + '\nProdutos sem verificação de origem: ' + (d.products_without_origin_check || 0) + '\nOrigem nacional confirmada: ' + (d.accepted_national || 0) + '\nOrigem desconhecida excluída: ' + (d.excluded_unknown_origin || 0) + '\nAviso de importação internacional encontrado: ' + (d.international_notices_found || 0) + '\nInternacionais excluídos: ' + (d.excluded_international || 0) + '\nLinks novos salvos: ' + (d.new_items || 0) + '\nErros: ' + (d.errors || 0) + '\nIntervalo: ' + Math.round((d.interval_seconds || 600) / 60) + ' min · Limite: ' + (d.max_new_products || 5) + '\n' + (d.warning || 'Filtro nacional aplicado.') + '\nColeta automática: ' + (d.automatic_collection_enabled ? 'ligada' : 'desligada');
  });
});
refresh();
</script>
</body>
</html>""")
