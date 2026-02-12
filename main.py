import os
import asyncio
import logging
import json
import base64
import aiohttp
import nest_asyncio

from pathlib import Path
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramConflictError
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

# Попытка импортировать класс для отправки локального файла (совместимо с разными версиями aiogram)
try:
    from aiogram.types import FSInputFile  # aiogram v3
except Exception:
    from aiogram.types import InputFile as FSInputFile  # fallback

router = Router()


# если запускаешь в Jupyter — применим фикс
try:
    nest_asyncio.apply()
except Exception:
    pass

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# ====== Конфигурация (заполни переменными окружения или прямо тут) ======
# Загружаем файл tgbot.env из той же папки, где лежит main.py (если есть)
env_path = Path(__file__).resolve().parent / "tgbot.env"
if env_path.exists():
    load_dotenv(dotenv_path=env_path)

# Берём секреты из окружения (или из tgbot.env, если он загружен)
TOKEN = os.getenv("TG_BOT_TOKEN", "")
INTEL_API_KEY = os.getenv("INTEL_API_KEY", "")
YCLIENTS_URL = os.getenv("YCLIENTS_URL",
    "https://yandex.ru/maps/org/bright/45615631345/?booking%5Bpage%5D=services&booking%5Bpermalink%5D=45615631345&booking%5BresourceId%5D=2756658&ll=37.720419%2C55.893761&z=15"
)
if not TOKEN:
    raise ValueError("TG_BOT_TOKEN must be set")
# Данные о мастере (не менял текстовые поля)
MASTER_NAME = "Ангелина"
SALON_NAME = "Bright"
ADDRESS = "г. Мытищи, ул. Веры Волошиной, 19/16, этаж 2, офис 255"

SYSTEM_PROMPT = f"""
Ты — умная помощница мастера Ангелины в студии {SALON_NAME}. 
Твоя цель: отвечать на вопросы клиенток.
ПРАВИЛА:
1. Ты мастер по ресницам, бровям и депиляции.
2. Если просят записаться — давай ссылку: {YCLIENTS_URL}
3. Отвечай коротко, используй эмодзи ✨, 🌸.
4. Тон: дружелюбный, профессиональный, на "Вы".
Если тебе пришлют фото, проанализируй его как эксперт по красоте.
"""

# Модель — установи доступную в твоём аккаунте intelligence.io
INTEL_MODEL = "deepseek-ai/DeepSeek-R1-0528"
INTEL_API_URL = "https://api.intelligence.io.solutions/api/v1/chat/completions"

# ====== Функция запроса к intelligence API ======
async def get_ai_answer(prompt: str, img_bytes: bytes | None = None):
    if not INTEL_API_KEY:
        log.error("INTEL_API_KEY не задан.")
        return None

    headers = {
        "Authorization": f"Bearer {INTEL_API_KEY}",
        "Content-Type": "application/json",
    }

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": prompt}
    ]

    payload = {
        "model": INTEL_MODEL,
        "messages": messages,
        "max_tokens": 512,
        "temperature": 0.7,
        "n": 1,
        "stream": False,
    }

    if img_bytes is not None:
        try:
            b64 = base64.b64encode(img_bytes).decode("ascii")
            payload["documents"] = [
                {"name": "image.jpg", "mime_type": "image/jpeg", "data_base64": b64}
            ]
            payload["messages"].append({
                "role": "user",
                "content": "Внимательно проанализируй присланную фотографию и дай рекомендацию как бьюти-эксперт."
            })
        except Exception as e:
            log.exception("Ошибка кодирования изображения: %s", e)
            # продолжаем без документа

    timeout = aiohttp.ClientTimeout(total=60)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(INTEL_API_URL, headers=headers, json=payload) as resp:
                text = await resp.text()
                if resp.status != 200:
                    log.error("intelligence API returned status %s", resp.status)
                    log.debug("RAW RESPONSE: %s", text)
                    try:
                        j = json.loads(text)
                        log.debug("PARSED: %s", json.dumps(j, ensure_ascii=False, indent=2))
                    except Exception:
                        pass
                    return None

                try:
                    j = json.loads(text)
                except Exception:
                    log.exception("Не удалось распарсить JSON ответ.")
                    return None

                # Парсим ответ (несколько вариантов)
                if isinstance(j, dict) and 'choices' in j and j['choices']:
                    first = j['choices'][0]
                    msg = first.get('message') or {}
                    content = msg.get('content')
                    if content:
                        return content
                    alt = first.get('text')
                    if alt:
                        return alt

                for key in ("output", "outputs", "generated_text", "result", "text"):
                    v = j.get(key)
                    if isinstance(v, str) and v.strip():
                        return v
                    if isinstance(v, list) and v:
                        if isinstance(v[0], dict):
                            for subkey in ("text", "content", "generated_text"):
                                if subkey in v[0]:
                                    return v[0][subkey]
                        elif isinstance(v[0], str):
                            return v[0]

                log.warning("Нестандартная структура ответа от intelligence API: %s", json.dumps(j, ensure_ascii=False)[:1000])
                return None

    except Exception as e:
        log.exception("Критическая ошибка запроса к intelligence API: %s", e)
        return None

# ====== Логика бота ======
if not TOKEN:
    log.warning("TG_BOT_TOKEN не задан. Убедись, что переменная окружения TG_BOT_TOKEN выставлена.")
bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN))
dp = Dispatcher()
dp.include_router(router)

main_menu = ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text="📅 Записаться онлайн")],
    [KeyboardButton(text="❓ Вопрос по уходу"), KeyboardButton(text="📍 Адрес салона")]
], resize_keyboard=True)

booking_button = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="✨ Выбрать время и услугу", url=YCLIENTS_URL)]
])

@dp.message(CommandStart())
async def start(message: types.Message):
    name_genitive = MASTER_NAME[:-1] + "ы" if MASTER_NAME.endswith("а") else MASTER_NAME
    await message.answer(
        f"Привет, красотка! ✨\n\nЯ — помощница мастера {name_genitive}. "
        "Помогу тебе записаться, найти студию или отвечу на любые вопросы по процедурам.\n\n"
        "Выбирай нужный пункт в меню или просто напиши мне свой вопрос! 👇",
        reply_markup=main_menu
    )

@dp.message(F.text == "📅 Записаться онлайн")
async def go_to_booking(message: types.Message):
    await message.answer("Выбирай удобную услугу и свободное окошко прямо здесь: 👇", reply_markup=booking_button)

@dp.message(F.text == "📍 Адрес салона")
async def show_address(message: types.Message):
    await message.answer(
        f"📍 **Наш адрес:**\n{ADDRESS}\n\n"
        "Приходи за 5 минут до начала, чтобы настроиться на красоту! ✨",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🗺 Открыть карту", url="https://yandex.ru/maps/-/CDQIqsQA")]
        ])
    )

@dp.message(F.text == "❓ Вопрос по уходу")
async def care_manual(message: types.Message):
    text = (
        "🌸 **Краткая памятка по уходу:**\n\n"
        "• Чтобы сохранить правильное положение ресниц на ресничном крае желательно не прикасаться к глазам и не чесать их. 💧\n"
        "• Умывание лучше перенести на следующий день после наращивания. Бассейн — только на 3 сутки. 🧴\n"
        "• Тщательно мой руки, чтобы избежать инфекции. ☁️\n"
        "• Следи за состоянием глаз.\n\n"
        "✨**Если есть вопрос (например, 'можно ли в баню?'), напиши его сюда!**\n"
    )
    
    # Пытаемся отправить локальный файл pigie.png (ищем в папке static рядом с main.py)
    pic_path = Path(__file__).resolve().parent / "static" / "pigie.png"
    try:
        if pic_path.exists():
            photo_file = FSInputFile(str(pic_path))
            await message.answer_photo(photo=photo_file, caption=text)
        else:
            raise FileNotFoundError(f"{pic_path} not found")
    except Exception as e:
        log.error(f"Не удалось найти или отправить файл pigie.png: {e}")
        # Если файл не найден или другая ошибка — отправим просто текст, чтобы бот не молчал
        await message.answer(text)
        
LOVE_TEXT = """
Линочка, моя любимочка 💕

Если ты читаешь это, значит ты нашла секретную команду.

Я хочу, чтобы ты знала:
ты самое нежное, самое родное и самое светлое,
что случилось со мной.

Этот бот мой маленький подарок для тебя,
потому что ты самая любимая девочка.

Я люблю тебя. 💖
"""
@router.message(Command("1437"))
async def secret_love_handler(message: Message):
    await message.answer(LOVE_TEXT)

VIP_SONG_TEXT = """ТВОЙ ПАРЕНЬ ЗАЁРЗАЛ РЯДОМ С VIP ПЕРСОН, МАЛЫШ НЕ ПЛАЧЬ, Я НЕСЕРЬЁЗНО
"""

@router.message(Command("vip_наращивание"))
async def vip_song_handler(message: Message):
    await message.answer(VIP_SONG_TEXT)

@dp.message(F.photo)
async def handle_photo(message: types.Message):
    await bot.send_chat_action(chat_id=message.chat.id, action="typing")
    photo = message.photo[-1]
    file = await bot.get_file(photo.file_id)
    photo_bytes = await bot.download_file(file.file_path)

    # безопасное чтение байтов
    if hasattr(photo_bytes, "read"):
        img_data = photo_bytes.read()
    elif isinstance(photo_bytes, (bytes, bytearray)):
        img_data = bytes(photo_bytes)
    else:
        img_data = b""

    prompt = message.caption if message.caption else "Что ты видишь на этом фото? Проконсультируй как мастер."
    answer = await get_ai_answer(prompt, img_data)

    if answer:
        await message.answer(answer, reply_markup=booking_button)
    else:
        await message.answer("Не удалось рассмотреть фото... Попробуй еще раз! ✨")

@dp.message(F.text & ~F.text.startswith("/"))
async def talk_to_ai(message: types.Message):
    await bot.send_chat_action(chat_id=message.chat.id, action="typing")
    answer = await get_ai_answer(message.text)
    if answer:
        await message.answer(answer, reply_markup=booking_button)
    else:
        await message.answer(
            "Я немного отвлеклась на подготовку ресничек... 👁️✨\n"
            "Попробуй повторить вопрос или нажми на запись!",
            reply_markup=booking_button
        )

# ====== Запуск (один main, с защитой от конфликта) ======
async def main():
    log.info("Бот ANGELASHES запущен!")
    # пробуем удалить webhook и сбросить pending updates
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        log.info("Попытка удалить webhook выполнена (drop_pending_updates=True).")
    except Exception as e:
        log.warning("Не удалось удалить webhook (возможно, его нет): %s", e)

    # запускаем polling с обработкой конфликта
    try:
        await dp.start_polling(bot)
    except TelegramConflictError as e:
        log.error("ConflictError при start_polling: %s", e)
        # пробуем ещё раз удалить webhook и перезапустить один раз
        try:
            await bot.delete_webhook(drop_pending_updates=True)
            log.info("Удалили webhook, пробуем перезапустить polling ещё раз.")
            await dp.start_polling(bot)
        except Exception as e2:
            log.exception("Перезапуск polling не удался: %s", e2)

# Универсальный запуск: работает в обычном Python и в Jupyter
if __name__ == "__main__":
    try:
        asyncio.run(main())
    except RuntimeError as e:
        # скорее всего: "This event loop is already running" (Jupyter)
        log.warning("asyncio.run failed (%s). Используем loop.create_task (Jupyter mode).", e)
        loop = asyncio.get_event_loop()
        loop.create_task(main())
