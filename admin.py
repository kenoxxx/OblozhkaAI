import logging
from aiogram import Router, Bot, F
from aiogram.filters import Command
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.enums import ParseMode

from supabase import Client, create_client
from config import settings

supabase: Client = create_client(settings.supabase_url, settings.supabase_key)
logger = logging.getLogger(__name__)

ADMIN_ID = 8701563086

admin_router = Router()


class AdminStates(StatesGroup):
    waiting_user_id = State()


def kb_admin() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔍 Поиск пользователя", callback_data="admin_search")],
        [InlineKeyboardButton(text="💰 Баланс бота (звёзды)", callback_data="admin_bot_balance")],
    ])


@admin_router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.clear()
    await message.answer(
        "🛠 <b>Админ-панель</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=kb_admin(),
    )


@admin_router.callback_query(F.data == "admin_search")
async def cb_admin_search(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    await callback.answer()
    await state.set_state(AdminStates.waiting_user_id)
    await callback.message.answer("🔍 Введите <b>user_id</b> пользователя:", parse_mode=ParseMode.HTML)


@admin_router.message(AdminStates.waiting_user_id)
async def handle_user_id_input(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return

    text = (message.text or "").strip()
    if not text.isdigit():
        await message.answer("⚠️ Введите числовой user_id.")
        return

    user_id = int(text)
    res = supabase.table("users").select("*").eq("user_id", user_id).execute()

    if not res.data:
        await message.answer(f"❌ Пользователь <code>{user_id}</code> не найден.", parse_mode=ParseMode.HTML)
        await state.clear()
        return

    u = res.data[0]
    info = (
        f"👤 <b>Пользователь</b>: <code>{u.get('user_id')}</code>\n"
        f"📛 Username: @{u.get('username') or '—'}\n"
        f"💰 Баланс: <b>{u.get('balance', 0)}</b>\n"
        f"🎨 Генераций использовано: {u.get('generations_used', 0)}\n"
        f"👥 Реферер: {u.get('referrer_id') or '—'}\n"
        f"📅 Регистрация: {str(u.get('created_at', ''))[:10]}"
    )

    await message.answer(info, parse_mode=ParseMode.HTML, reply_markup=kb_admin())
    await state.clear()


@admin_router.callback_query(F.data == "admin_bot_balance")
async def cb_admin_bot_balance(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    await callback.answer()
    try:
        balance = await callback.bot.get_star_transactions(limit=1)
        await callback.message.answer(
            f"💰 <b>Транзакции бота получены.</b>\nИспользуйте @BotFather для проверки баланса звёзд.",
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        await callback.message.answer(f"❌ Ошибка: {e}")
