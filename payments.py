import logging
from aiogram import Router, F, Bot
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    PreCheckoutQuery,
    Message,
    LabeledPrice,
)
from aiogram.enums import ParseMode

from supabase import Client, create_client
from config import settings

supabase: Client = create_client(settings.supabase_url, settings.supabase_key)
payments_router = Router()
logger = logging.getLogger(__name__)

# Тарифы (пакет: цена в звездах)
PACKAGES = {
    "buy_1_gen": {"amount": 1, "price": 20, "label": "1 генерация"},
    "buy_5_gens": {"amount": 5, "price": 65, "label": "5 генераций"},
    "buy_20_gens": {"amount": 20, "price": 220, "label": "20 генераций"},
    "buy_60_gens": {"amount": 60, "price": 500, "label": "60 генераций"},
}

def kb_tariffs() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text=f"⭐️ {pkg['label']} — {pkg['price']} XTR", callback_data=key)]
        for key, pkg in PACKAGES.items()
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

@payments_router.callback_query(F.data == "buy_generations")
async def cb_show_tariffs(callback: CallbackQuery) -> None:
    await callback.answer()
    
    promo_text = (
        "💎 <b>Премиальные генерации Обложка AI</b>\n\n"
        "В отличие от обычных ботов с шаблонными картинками, мы используем "
        "эксклюзивную связку из двух закрытых ИИ-моделей.\n"
        "Бот не просто рисует картинку, а <b>глубоко анализирует ДНК дизайна "
        "вашего канала</b>, копируя стиль, цветовую гамму и типографику ваших предыдущих обложек.\n\n"
        "<i>Это делает каждую работу максимально релевантной и пробивает баннерную слепоту, "
        "увеличивая кликабельность (CTR) вашего ролика до максимума.</i>\n\n"
        "Выберите пакет:"
    )
    
    await callback.message.answer(promo_text, parse_mode=ParseMode.HTML, reply_markup=kb_tariffs())

@payments_router.callback_query(F.data.in_(PACKAGES.keys()))
async def cb_buy_package(callback: CallbackQuery) -> None:
    await callback.answer()
    pkg_key = callback.data
    pkg = PACKAGES[pkg_key]
    
    prices = [LabeledPrice(label=pkg["label"], amount=pkg["price"])]
    
    await callback.message.answer_invoice(
        title=f"Пакет: {pkg['label']} 🎨",
        description=f"Покупка {pkg['amount']} премиум-генераций для Обложка AI.",
        payload=pkg_key,
        provider_token="", # Для Telegram Stars (XTR)
        currency="XTR",
        prices=prices,
    )

@payments_router.pre_checkout_query()
async def process_pre_checkout_query(pre_checkout_query: PreCheckoutQuery):
    await pre_checkout_query.answer(ok=True)

@payments_router.message(F.successful_payment)
async def process_successful_payment(message: Message, bot: Bot):
    payload = message.successful_payment.invoice_payload
    user_id = message.from_user.id
    
    if payload not in PACKAGES:
        return

    amount_bought = PACKAGES[payload]["amount"]
    
    # 1. Пополнение баланса
    res = supabase.table("users").select("*").eq("user_id", user_id).execute()
    if res.data:
        user_record = res.data[0]
        current_balance = user_record.get("balance", 0)
        new_balance = current_balance + amount_bought
        
        supabase.table("users").update({"balance": new_balance}).eq("user_id", user_id).execute()
        
        await message.answer(
            f"✅ <b>Оплата прошла успешно!</b>\n"
            f"Вам начислено: <b>{amount_bought}</b> генераций.\n"
            f"Ваш текущий баланс: <b>{new_balance}</b>.",
            parse_mode=ParseMode.HTML
        )
        
        # 2. Проверка реферальной системы (защита от абуза)
        referrer_id = user_record.get("referrer_id")
        bonus_received = user_record.get("bonus_received", False)
        
        if referrer_id and not bonus_received:
            # Начисляем рефереру +3
            ref_res = supabase.table("users").select("balance").eq("user_id", referrer_id).execute()
            if ref_res.data:
                ref_balance = ref_res.data[0].get("balance", 0)
                supabase.table("users").update({"balance": ref_balance + 3}).eq("user_id", referrer_id).execute()
                
                # Отмечаем, что бонус за этого юзера выдан
                supabase.table("users").update({"bonus_received": True}).eq("user_id", user_id).execute()
                
                # Уведомляем пригласившего
                try:
                    await bot.send_message(
                        referrer_id, 
                        (
                            "🎉 <b>Ваш друг совершил первую покупку!</b>\n"
                            "В знак благодарности мы начислили вам <b>+3 бонусные генерации</b>. "
                            "Спасибо, что рекомендуете нас!"
                        ),
                        parse_mode=ParseMode.HTML
                    )
                except Exception as e:
                    logger.error(f"Cannot send bonus notification to {referrer_id}: {e}")
