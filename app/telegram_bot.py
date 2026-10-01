import logging
from datetime import datetime, timezone
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, ContextTypes
from app import config
from app.db import get_db, log_event

log = logging.getLogger(__name__)

def is_admin(user_id: int | None) -> bool:
    return user_id is not None and user_id in config.TELEGRAM_ADMIN_IDS

async def send_pending_products(app: Application, limit: int = 10) -> int:
    if not config.telegram_ready() or not config.supabase_ready():
        return 0
    db = get_db()
    rows = db.table("extrator_products").select("*").eq("status", "pending_approval").is_("review_message_id", "null").order("discovered_at").limit(limit).execute().data or []
    sent = 0
    admin_chat_id = sorted(config.TELEGRAM_ADMIN_IDS)[0]
    for p in rows:
        caption = f"<b>{escape_html(p['title'])}</b>\n"
        if p.get("price") is not None:
            caption += f"Preço informado pela API: R$ {float(p['price']):.2f}\n"
        caption += f"\n<a href=\"{escape_html(p['permalink'])}\">Abrir produto no Mercado Livre</a>\n\nID: <code>{p['item_id']}</code>"
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Aprovar", callback_data=f"approve:{p['id']}"),
            InlineKeyboardButton("❌ Reprovar", callback_data=f"reject:{p['id']}"),
        ]])
        try:
            if p.get("thumbnail"):
                msg = await app.bot.send_photo(chat_id=admin_chat_id, photo=p["thumbnail"], caption=caption, parse_mode="HTML", reply_markup=keyboard)
            else:
                msg = await app.bot.send_message(chat_id=admin_chat_id, text=caption, parse_mode="HTML", reply_markup=keyboard, disable_web_page_preview=False)
            db.table("extrator_products").update({
                "review_chat_id": msg.chat_id, "review_message_id": msg.message_id
            }).eq("id", p["id"]).eq("status", "pending_approval").execute()
            sent += 1
        except Exception as exc:
            log.warning("Falha ao enviar produto %s para revisão: %s", p["id"], exc)
    return sent

def escape_html(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

async def on_decision(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    user = query.from_user
    if not is_admin(user.id):
        await query.answer("Você não está autorizado a revisar produtos.", show_alert=True)
        return
    action, _, product_id = (query.data or "").partition(":")
    if action not in {"approve", "reject"} or not product_id:
        await query.answer("Ação inválida.", show_alert=True)
        return
    db = get_db()
    rows = db.table("extrator_products").select("*").eq("id", product_id).eq("status", "pending_approval").limit(1).execute().data or []
    if not rows:
        await query.answer("Este produto já foi decidido ou não está disponível.", show_alert=True)
        return
    product = rows[0]
    new_status = "approved" if action == "approve" else "rejected"
    changed = db.table("extrator_products").update({
        "status": new_status, "reviewed_by": user.id, "reviewed_at": datetime.now(timezone.utc).isoformat()
    }).eq("id", product_id).eq("status", "pending_approval").execute().data or []
    if not changed:
        await query.answer("A decisão já foi registrada.", show_alert=True)
        return
    log_event(f"product_{new_status}", product_id, user.id, {"item_id": product["item_id"]})
    if action == "reject":
        await query.answer("Produto reprovado.")
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(f"❌ Reprovado: {product['title']}")
        return
    try:
        db.table("extrator_products").update({"status": "publishing"}).eq("id", product_id).eq("status", "approved").execute()
        caption = f"<b>{escape_html(product['title'])}</b>\n"
        if product.get("price") is not None:
            caption += f"Preço informado pela API: R$ {float(product['price']):.2f}\n"
        caption += f"\n<a href=\"{escape_html(product['permalink'])}\">🛒 Ver oferta no Mercado Livre</a>"
        if product.get("thumbnail"):
            published = await context.bot.send_photo(chat_id=config.TELEGRAM_CHANNEL_ID, photo=product["thumbnail"], caption=caption, parse_mode="HTML")
        else:
            published = await context.bot.send_message(chat_id=config.TELEGRAM_CHANNEL_ID, text=caption, parse_mode="HTML")
        db.table("extrator_products").update({
            "status": "published", "channel_message_id": published.message_id, "published_at": datetime.now(timezone.utc).isoformat()
        }).eq("id", product_id).execute()
        log_event("product_published", product_id, user.id, {"channel_message_id": published.message_id})
        await query.answer("Oferta publicada no canal.")
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(f"✅ Publicado: {product['title']}")
    except Exception as exc:
        db.table("extrator_products").update({"status": "publish_failed"}).eq("id", product_id).execute()
        log_event("publication_failed", product_id, user.id, {"error": str(exc)[:500]})
        await query.answer("Falha ao publicar. O erro foi registrado.", show_alert=True)
        await query.message.reply_text("⚠️ A publicação falhou. Verifique os logs antes de tentar novamente.")

async def start_bot():
    application = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()
    application.add_handler(CallbackQueryHandler(on_decision, pattern=r"^(approve|reject):"))
    await application.initialize()
    await application.start()
    await application.updater.start_polling(drop_pending_updates=False)
    return application
