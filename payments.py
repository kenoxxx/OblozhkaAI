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

# ─── Пакеты Telegram Stars (XTR) ───
PACKAGES = {
    "buy_1_gen":  {"amount": 1,   "price": 20,   "label": "⚡️ 1 генерация",          "desc": "1 генерация"},
    "buy_week":   {"amount": 15,  "price": 150,  "label": '📅 Пакет "Неделя" (15 шт)', "desc": "15 генераций"},
    "buy_month":  {"amount": 70,  "price": 500,  "label": '🗓 Пакет "Месяц" (70 шт)',  "desc": "70 генераций"},
    "buy_expert": {"amount": 150, "price": 1000, "label": "🔥 Эксперт (150 шт)",       "desc": "150 генераций"},
}


def kb_tariffs() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text=f"{pkg['label']} — {pkg['price']} ⭐", callback_data=key)]
        for key, pkg in PACKAGES.items()
    ]
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_main")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@payments_router.callback_query(F.data == "open_shop")
async def cb_show_tariffs(callback: CallbackQuery) -> None:
    await callback.answer()

    promo_text = (
        "💎 <b>Магазин генераций</b>\n\n"
        "В отличие от обычных ботов с шаблонными картинками, мы используем "
        "эксклюзивную связку из двух ИИ-моделей.\n"
        "Бот <b>глубоко анализирует ДНК дизайна вашего канала</b>, копируя "
        "стиль, цветовую гамму и типографику ваших обложек.\n\n"
        "Выберите пакет:"
    )

    await callback.message.answer(promo_text, parse_mode=ParseMode.HTML, reply_markup=kb_tariffs())


@payments_router.callback_query(F.data.in_(PACKAGES.keys()))
async def cb_buy_package(callback: CallbackQuery) -> None:
    await callback.answer()
    pkg = PACKAGES[callback.data]

    prices = [LabeledPrice(label=pkg["desc"], amount=pkg["price"])]

    await callback.message.answer_invoice(
        title=f"{pkg['label']} 🎨",
        description=f"Покупка {pkg['desc']} для Обложка AI.",
        payload=callback.data,
        provider_token="",
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

    res = supabase.table("users").select("*").eq("user_id", user_id).execute()
    if res.data:
        user_record = res.data[0]
        current_balance = user_record.get("balance", 0)
        new_balance = current_balance + amount_bought

        supabase.table("users").update({"balance": new_balance}).eq("user_id", user_id).execute()

        await message.answer(
            f"✅ <b>Оплата прошла успешно!</b>\n"
            f"Начислено: <b>{amount_bought}</b> генераций.\n"
            f"Текущий баланс: <b>{new_balance}</b>.",
            parse_mode=ParseMode.HTML,
        )

        # Реферальный бонус (однократно)
        referrer_id = user_record.get("referrer_id")
        bonus_received = user_record.get("bonus_received", False)

        if referrer_id and not bonus_received:
            ref_res = supabase.table("users").select("balance").eq("user_id", referrer_id).execute()
            if ref_res.data:
                ref_balance = ref_res.data[0].get("balance", 0)
                supabase.table("users").update({"balance": ref_balance + 3}).eq("user_id", referrer_id).execute()
                supabase.table("users").update({"bonus_received": True}).eq("user_id", user_id).execute()

                try:
                    await bot.send_message(
                        referrer_id,
                        "🎉 <b>Ваш друг совершил первую покупку!</b>\n"
                        "Вам начислено <b>+3 бонусные генерации</b>!",
                        parse_mode=ParseMode.HTML,
                    )
                except Exception as e:
                    logger.error(f"Cannot send bonus notification to {referrer_id}: {e}")
