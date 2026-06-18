# ============================================================================
# ИСХОДНЫЙ КОД TELEGRAM-БОТА (main.py)
# Фреймворк: aiogram v3.x (Python 3.10+) | Июнь 2026
# Полная интеграция с PostgreSQL и Google Sheets CRM
# ============================================================================

import os
import asyncio
import logging
import json
from datetime import datetime
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

import asyncpg
import gspread
from google.oauth2.service_account import Credentials

# Загрузка переменных окружения
load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
MASTER_ID = os.getenv("MASTER_TELEGRAM_ID")
DATABASE_URL = os.getenv("DATABASE_URL")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME", "Leads & Orders")
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")

if not BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN не найден в переменных окружения .env!")

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN, parse_mode="HTML")
dp = Dispatcher()
router = Router()

# ==========================================
# ПОДКЛЮЧЕНИЕ К CRM (POSTGRES / SHEETS)
# ==========================================
async def save_lead_to_postgres(lead_id: str, client_name: str, phone: str, pet: str, dimensions: str, comment: str):
    if not DATABASE_URL:
        return
    try:
        conn = await asyncpg.connect(DATABASE_URL)
        await conn.execute(
            """
            INSERT INTO leads (lead_id, source, client_name, contact_info, preferred_messenger, pet_type, dimensions, comment, status)
            VALUES ($1, 'TelegramBot', $2, $3, 'telegram', $4, $5, $6, 'NEW_LEAD')
            ON CONFLICT (lead_id) DO NOTHING;
            """,
            lead_id, client_name, phone, pet, dimensions, comment
        )
        await conn.close()
        logging.info(f"Лид {lead_id} успешно сохранен в PostgreSQL.")
    except Exception as e:
        logging.error(f"Ошибка сохранения лида в PostgreSQL: {e}")

async def update_status_in_postgres(lead_id: str, new_status: str):
    if not DATABASE_URL:
        return
    try:
        conn = await asyncpg.connect(DATABASE_URL)
        await conn.execute("UPDATE leads SET status = $1 WHERE lead_id = $2;", new_status, lead_id)
        await conn.close()
        logging.info(f"Статус лида {lead_id} обновлен на {new_status} в PostgreSQL.")
    except Exception as e:
        logging.error(f"Ошибка обновления статуса в PostgreSQL: {e}")

async def save_lead_to_sheets(lead_id: str, client_name: str, phone: str, pet: str, dimensions: str, comment: str):
    """Асинхронная обертка для сохранения в Google Sheets"""
    if not GOOGLE_SHEET_ID or not GOOGLE_SERVICE_ACCOUNT_JSON:
        return
    try:
        await asyncio.to_thread(_sync_save_to_sheets, lead_id, client_name, phone, pet, dimensions, comment)
    except Exception as e:
        logging.error(f"Ошибка сохранения лида в Google Sheets: {e}")

def _sync_save_to_sheets(lead_id: str, client_name: str, phone: str, pet: str, dimensions: str, comment: str):
    """Синхронная функция для работы с Google Sheets"""
    try:
        creds_data = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
        scopes = ['https://www.googleapis.com/auth/spreadsheets']
        creds = Credentials.from_service_account_info(creds_data, scopes=scopes)
        client = gspread.authorize(creds)
        sheet = client.open_by_key(GOOGLE_SHEET_ID).worksheet(GOOGLE_SHEET_NAME)
        
        # Запись строки (16 колонок по структуре Google Sheets)
        row_data = [
            lead_id,
            datetime.now().strftime("%d.%m.%Y %H:%M"),
            "TelegramBot",
            client_name,
            phone,
            "telegram",
            pet,
            dimensions,
            "Кастом / Бот",
            comment,
            "🟡 NEW_LEAD",
            "", "", "", "", ""
        ]
        sheet.append_row(row_data)
        logging.info(f"Лид {lead_id} успешно добавлен в Google Sheets.")
    except Exception as e:
        logging.error(f"Ошибка сохранения лида в Google Sheets: {e}")

# ==========================================
# FSM СТЕЙТЫ ДЛЯ ЗАЯВКИ
# ==========================================
class LeadForm(StatesGroup):
    waiting_for_pet = State()
    waiting_for_dimensions = State()
    waiting_for_wishes = State()
    waiting_for_contact = State()

# ==========================================
# КЛАВИАТУРЫ
# ==========================================
def get_main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📋 Популярные пакеты и цены")],
            [KeyboardButton(text="📐 Заказать по своим размерам")],
            [KeyboardButton(text="❓ FAQ и Доставка"), KeyboardButton(text="📞 Связь с мастером")]
        ],
        resize_keyboard=True
    )

def get_admin_keyboard(lead_id: str):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🟠 Смета", callback_data=f"status_ESTIMATING_{lead_id}"),
                InlineKeyboardButton(text="🔵 Аванс", callback_data=f"status_PREPAYMENT_{lead_id}")
            ],
            [
                InlineKeyboardButton(text="🟣 В производстве", callback_data=f"status_PRODUCTION_{lead_id}"),
                InlineKeyboardButton(text="🟢 Готов", callback_data=f"status_READY_{lead_id}")
            ],
            [
                InlineKeyboardButton(text="🏁 Завершить заказ", callback_data=f"status_COMPLETED_{lead_id}")
            ]
        ]
    )

# ==========================================
# ХЭНДЛЕРЫ КЛИЕНТА (ГЛАВНОЕ МЕНЮ)
# ==========================================
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    welcome_text = (
        f"Здравствуйте, <b>{message.from_user.first_name}</b>! 🦎\n\n"
        "Добро пожаловать в частную мастерскую <b>CRAFT TERRARIUMS</b> (Москва, 2026).\n\n"
        "Я создаю надежные, безопасные и эстетичные террариумы из кристального стекла М1 с полированной евро кромкой. "
        "Каждый террариум изготавливается вручную, с любовью к животному и соблюдением всех норм безопасности.\n\n"
        "Выберите интересующий вас раздел в меню ниже 👇"
    )
    await message.answer(welcome_text, reply_markup=get_main_keyboard())

@router.message(F.text == "📋 Популярные пакеты и цены")
async def show_packages(message: Message):
    packages_text = (
        "🔥 <b>ПОПУЛЯРНЫЕ ПАКЕТЫ УСЛУГ (Июнь 2026):</b>\n\n"
        "1️⃣ <b>Индивидуальный Кастом (от 4 000 руб.)</b>\n"
        "Изготовление стеклянного объема строго по вашим размерам или чертежам. Стекло М1 4–6 мм, станочная полировка кромок, герметичный силикон 100%. Срок: 5–7 дней.\n\n"
        "2️⃣ <b>Комплекс «Под ключ» (от 12 000 руб.) — ХИТ 🦎</b>\n"
        "Готовая экосистема «Заселяй и живи». Включает террариум, смонтированный УФ-свет, обогрев с терморегулятором, подложку, декор и 3D-фон. Полная консультация.\n\n"
        "3️⃣ <b>Флорариумы и Палюдариумы (от 18 000 руб.)</b>\n"
        "Тропический рай с герметичным дном под водоем, тихим встроенным водопадом и живыми тропическими растениями. Для древесных лягушек, мхов, тритонов.\n\n"
        "👉 Чтобы рассчитать точную цену под ваш проект, нажмите кнопку <i>«Заказать по своим размерам»</i>."
    )
    await message.answer(packages_text, reply_markup=get_main_keyboard())

@router.message(F.text == "❓ FAQ и Доставка")
async def show_faq(message: Message):
    faq_text = (
        "❓ <b>ЧАСТЫЕ ВОПРОСЫ:</b>\n\n"
        "📍 <b>Где находится мастерская?</b>\n"
        "Москва, м. Преображенская площадь. Доступен самовывоз.\n\n"
        "🚚 <b>Есть ли доставка?</b>\n"
        "Да, осуществляю личную бережную автодоставку по Москве и Подмосковью «до двери» (от 1000 руб. в зависимости от размера и расстояния).\n\n"
        "⌛️ <b>Какие сроки изготовления?</b>\n"
        "Обычно от 5 до 10 календарных дней.\n\n"
        "🛡 <b>Какая гарантия?</b>\n"
        "Предоставляю полную личную гарантию 3 года на герметичность и прочность всех клеевых швов."
    )
    await message.answer(faq_text, reply_markup=get_main_keyboard())

@router.message(F.text == "📞 Связь с мастером")
async def show_contacts(message: Message):
    contacts_text = (
        "👨‍🔧 <b>Контакты частного мастера:</b>\n\n"
        "Вы можете написать мне напрямую по любым вопросам, я с радостью проконсультирую!\n\n"
        "📲 <b>Telegram:</b> @YourTelegramUsername\n"
        "📞 <b>WhatsApp / Телефон:</b> +7 (999) XXX-XX-XX\n"
        "📍 <b>Мастерская:</b> Москва, ул. Примерная, д. 10"
    )
    await message.answer(contacts_text, reply_markup=get_main_keyboard())

# ==========================================
# ПОШАГОВЫЙ ОПРОСНИК (СОЗДАНИЕ ЛИДА)
# ==========================================
@router.message(F.text == "📐 Заказать по своим размерам")
async def start_lead_form(message: Message, state: FSMContext):
    await message.answer(
        "📝 Отлично! Давайте рассчитаем стоимость вашего будущего террариума.\n\n"
        "<b>Шаг 1 из 4:</b> Напишите, пожалуйста, для какого именно питомца планируется террариум? "
        "(Например: Эублефар, Кукуруза, Удав, Агама и т.д.)",
        reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="❌ Отмена")]], resize_keyboard=True)
    )
    await state.set_state(LeadForm.waiting_for_pet)

@router.message(F.text == "❌ Отмена")
async def cancel_form(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("❌ Заполнение заявки отменено.", reply_markup=get_main_keyboard())

@router.message(LeadForm.waiting_for_pet)
async def process_pet(message: Message, state: FSMContext):
    await state.update_data(pet=message.text)
    await message.answer(
        "📐 <b>Шаг 2 из 4:</b> Какие габариты (Длина х Ширина х Высота) вам нужны?\n\n"
        "<i>Если не уверены, напишите примерные или размеры места, куда планируете ставить террариум.</i>"
    )
    await state.set_state(LeadForm.waiting_for_dimensions)

@router.message(LeadForm.waiting_for_dimensions)
async def process_dimensions(message: Message, state: FSMContext):
    await state.update_data(dimensions=message.text)
    await message.answer(
        "🌿 <b>Шаг 3 из 4:</b> Нужен ли авторский рельефный 3D-фон (скалы/кора) и установка УФ-освещения?\n\n"
        "<i>Напишите ваши пожелания к внутреннему декору или цвету герметика (черный/прозрачный).</i>"
    )
    await state.set_state(LeadForm.waiting_for_wishes)

@router.message(LeadForm.waiting_for_wishes)
async def process_wishes(message: Message, state: FSMContext):
    await state.update_data(wishes=message.text)
    contact_kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📱 Поделиться номером телефона", request_contact=True)],
            [KeyboardButton(text="❌ Отмена")]
        ],
        resize_keyboard=True,
        one_time_keyboard=True
    )
    await message.answer(
        "📞 <b>Шаг 4 из 4:</b> Нажмите кнопку <b>«Поделиться номером телефона»</b> ниже, чтобы я мог связаться с вами в течение 30 минут с расчетом.\n\n"
        "<i>Также можете просто написать ваш номер телефона вручную.</i>",
        reply_markup=contact_kb
    )
    await state.set_state(LeadForm.waiting_for_contact)

@router.message(LeadForm.waiting_for_contact)
async def process_contact(message: Message, state: FSMContext):
    data = await state.get_data()
    pet = data.get("pet", "Не указан")
    dimensions = data.get("dimensions", "Не указаны")
    wishes = data.get("wishes", "Нет пожеланий")
    
    phone = message.contact.phone_number if message.contact else message.text
    client_name = message.from_user.first_name

    # Генерируем красивый ID
    lead_id = f"TG-{message.from_user.id}-{str(message.message_id)[:4]}"

    # 1. Запись в CRM
    await save_lead_to_postgres(lead_id, client_name, phone, pet, dimensions, wishes)
    await save_lead_to_sheets(lead_id, client_name, phone, pet, dimensions, wishes)

    # 2. Подтверждение клиенту
    success_text = (
        "🎉 <b>Спасибо за заявку!</b>\n\n"
        "Я лично получил все ваши параметры. Уже делаю расчет сметы и свяжусь с вами в течение 15–30 минут!\n\n"
        "<i>Пока ждете, можете заглянуть в мой канал с примерами работ: @YourChannelUsername</i>"
    )
    await message.answer(success_text, reply_markup=get_main_keyboard())
    await state.clear()

    # 3. Push-уведомление мастеру
    if MASTER_ID:
        master_text = (
            "🔥 <b>НОВАЯ ЗАЯВКА ИЗ БОТА (2026)!</b> 🔥\n\n"
            f"👤 <b>Клиент:</b> {client_name} (@{message.from_user.username or 'нет_юзернейма'})\n"
            f"📞 <b>Телефон:</b> <code>{phone}</code>\n\n"
            f"🦎 <b>Питомец:</b> {pet}\n"
            f"📐 <b>Размеры:</b> {dimensions}\n"
            f"💬 <b>Пожелания:</b> {wishes}\n\n"
            f"🆔 Lead ID: <code>{lead_id}</code>\n"
            f"📌 <b>Статус CRM:</b> <b>🟡 НОВАЯ ЗАЯВКА</b>"
        )
        try:
            await bot.send_message(
                chat_id=int(MASTER_ID),
                text=master_text,
                reply_markup=get_admin_keyboard(lead_id)
            )
        except Exception as e:
            logging.error(f"Не удалось отправить Push-уведомление мастеру: {e}")

# ==========================================
# ХЭНДЛЕРЫ АДМИНА (MINI-CRM CALLBACKS)
# ==========================================
@router.callback_query(F.data.startswith("status_"))
async def admin_status_update(callback: CallbackQuery):
    if str(callback.from_user.id) != MASTER_ID:
        await callback.answer("⛔️ У вас нет прав администратора!", show_alert=True)
        return

    parts = callback.data.split("_")
    action = parts[1] # ESTIMATING, PREPAYMENT, PRODUCTION, READY, COMPLETED
    lead_id = parts[2]

    status_map = {
        "ESTIMATING": ("🟠 В работе: Составление сметы", "ESTIMATING"),
        "PREPAYMENT": ("🔵 Получен аванс 50%", "PREPAYMENT_RECEIVED"),
        "PRODUCTION": ("🟣 В производстве (Склейка / Фон)", "IN_PRODUCTION"),
        "READY": ("🟢 Готов к выдачи / Отснят отчет", "READY_FOR_DELIVERY"),
        "COMPLETED": ("🏁 Заказ успешно завершен и оплачен", "COMPLETED")
    }

    display_status, db_status = status_map.get(action, ("Обновлен", "UPDATED"))
    
    # 1. Обновляем в PostgreSQL
    await update_status_in_postgres(lead_id, db_status)

    # 2. Обновляем текст сообщения мастера
    old_text = callback.message.html_text.split("📌 <b>Статус CRM:</b>")[0].strip()
    updated_msg = f"{old_text}\n\n📌 <b>Статус CRM:</b> <b>{display_status}</b>"

    await callback.message.edit_text(updated_msg, reply_markup=callback.message.reply_markup)
    await callback.answer(f"✅ Статус изменен: {display_status}")

# ==========================================
# MAIN LAUNCHER
# ==========================================
async def main():
    dp.include_router(router)
    logging.info("🚀 Python Telegram Bot полностью запущен и готов к приему заявок!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
