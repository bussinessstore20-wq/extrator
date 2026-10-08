import hashlib
import logging
from datetime import datetime, timezone
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes
from app import config
from app.db import get_db, log_event

log = logging.getLogger(__name__)

def is_admin(user_id: int | None) -> bool:
    return user_id is not None and user_id in config.TELEGRAM_ADMIN_IDS

def is_confirmed_national_shopee(product: dict) -> bool:
    if str(product.get("site_id") or "").upper() != "SHOPEE":
        return True
    metadata = product.get("metadata") or {}
    return metadata.get("shipping_origin_filter") == "national_confirmed"


async def send_pending_products(app: Application, limit: int = 10) -> int:
    if not config.telegram_ready() or not config.supabase_ready():
        return 0
    db = get_db()
    rows = db.table("extrator_products").select("*").eq("status", "pending_approval").is_("review_message_id", "null").order("discovered_at").limit(limit).execute().data or []
    sent = 0
    admin_chat_id = sorted(config.TELEGRAM_ADMIN_IDS)[0]
    for p in rows:
        is_shopee = str(p.get("site_id") or "").upper() == "SHOPEE"
        # Shopee items are only sent when national shipping is explicitly confirmed.
        if is_shopee and not is_confirmed_national_shopee(p):
            continue
        permalink = str(p.get("permalink") or "").strip()
        if not permalink:
            continue
        caption = permalink if is_shopee else f"🔗 <a href=\"{escape_html(permalink)}\">Abrir produto no Mercado Livre</a>\n\nID: <code>{p['item_id']}</code>"
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Aprovar", callback_data=f"approve:{p['id']}"),
            InlineKeyboardButton("❌ Reprovar", callback_data=f"reject:{p['id']}"),
        ]])
        try:
            if is_shopee:
                msg = await app.bot.send_message(chat_id=admin_chat_id, text=caption, reply_markup=keyboard, disable_web_page_preview=True)
            elif p.get("thumbnail"):
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


async def send_natura_collection_to_channel(app: Application, result: dict) -> int:
    """Envia imediatamente para o canal configurado os produtos obtidos na coleta pública da Natura."""
    if not config.telegram_ready() or not config.TELEGRAM_CHANNEL_ID:
        log.warning("Coleta Natura concluída, mas o Telegram/canal não está configurado.")
        return 0

    products = result.get("products") or []
    if not products:
        await app.bot.send_message(
            chat_id=config.TELEGRAM_CHANNEL_ID,
            text="🌿 <b>Coleta Natura</b>\\n\\nNenhum produto encontrado nesta coleta.",
            parse_mode="HTML",
        )
        return 0

    sent = 0
    for product in products:
        title = escape_html(str(product.get("title") or "Produto Natura"))
        price = escape_html(str(product.get("price") or "Preço não identificado"))
        old_price = escape_html(str(product.get("old_price") or "Preço anterior não identificado"))
        discount = escape_html(str(product.get("discount") or "Desconto não identificado"))
        availability = escape_html(str(product.get("availability") or "Disponibilidade não identificada"))
        permalink = str(product.get("permalink") or "").strip()
        image = str(product.get("image") or "").strip()

        lines = [
            "🌿 <b>OFERTA NATURA</b>",
            "",
            f"✨ <b>{title}</b>",
            "",
            f"💰 <b>Por: {price}</b>",
            f"🏷️ De: {old_price}",
            f"📉 Desconto: {discount}",
            f"📦 Disponibilidade: {availability}",
            "",
            f"🔗 <a href=\"{escape_html(permalink)}\">Comprar na Natura</a>" if permalink else "🔗 Link não identificado",
        ]
        caption = "\\n".join(lines)

        try:
            if image:
                await app.bot.send_photo(
                    chat_id=config.TELEGRAM_CHANNEL_ID,
                    photo=image,
                    caption=caption,
                    parse_mode="HTML",
                )
            else:
                await app.bot.send_message(
                    chat_id=config.TELEGRAM_CHANNEL_ID,
                    text=caption,
                    parse_mode="HTML",
                    disable_web_page_preview=False,
                )
            sent += 1
        except Exception as exc:
            log.warning("Falha ao enviar imagem da Natura para o canal: %s", exc)
            try:
                await app.bot.send_message(
                    chat_id=config.TELEGRAM_CHANNEL_ID,
                    text=caption,
                    parse_mode="HTML",
                    disable_web_page_preview=False,
                )
                sent += 1
            except Exception:
                log.exception("Falha definitiva ao enviar produto Natura para o canal.")

    return sent

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
    if not is_confirmed_national_shopee(product):
        await query.answer("Bloqueado: o envio nacional deste produto não foi confirmado.", show_alert=True)
        return
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
        is_shopee = str(product.get("site_id") or "").upper() == "SHOPEE"
        caption = product["permalink"] if is_shopee else f"🔗 <a href=\"{escape_html(product['permalink'])}\">🛒 Ver oferta no Mercado Livre</a>\n\nID: <code>{product['item_id']}</code>"
        if is_shopee:
            published = await context.bot.send_message(chat_id=config.TELEGRAM_CHANNEL_ID, text=caption, disable_web_page_preview=True)
        elif product.get("thumbnail"):
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

async def on_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message:
        await update.effective_message.reply_text(
            "🤖 Extrator conectado.\n\n"
            "Use /status para consultar o estado do serviço."
        )


async def on_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_message or not update.effective_user:
        return
    if not is_admin(update.effective_user.id):
        await update.effective_message.reply_text("Acesso restrito ao administrador.")
        return
    collector = "ligada" if config.COLLECTOR_ENABLED else "desligada"
    database = "conectado" if config.supabase_ready() else "não configurado"
    await update.effective_message.reply_text(
        "📊 Status do Extrator\n"
        f"Banco de dados: {database}\n"
        f"Coleta automática: {collector}\n"
        "Aprovação de produtos: disponível quando houver itens pendentes."
    )


def telegram_webhook_secret() -> str:
    value = f"{config.TELEGRAM_BOT_TOKEN}:extrator-webhook-v1"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


async def start_bot():
    application = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", on_start))
    application.add_handler(CommandHandler("status", on_status))
    application.add_handler(CallbackQueryHandler(on_decision, pattern=r"^(approve|reject):"))
    await application.initialize()
    await application.start()
    return application
