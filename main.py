"""
Telegram-бот "Обложка AI"

Стек:
- aiogram 3.x — Telegram-бот
- Supabase — хранение пользователей
- OpenRouter — Gemini 2.5 Flash Lite (анализ) + Claude 3.5 Sonnet (стратегия и тексты)
- Replicate — black-forest-labs/flux-dev (генерация)
- YouTube Data API v3 — метаданные видео
- Pillow — наложение русского текста

Поток:
  /start → ссылки → [анализ] → формат (16:9 / 9:16) → фото → тема видео
           → тригер → [3 варианта текста] → выбор → генерация → обложка
"""

import asyncio
import io
import json
import logging
import os
import re
import sys
from typing import Optional

import httpx
import logging as _logging
_logging.getLogger("googleapiclient.discovery_cache").setLevel(_logging.ERROR)
from googleapiclient.discovery import build
from PIL import Image, ImageDraw, ImageFont
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    PreCheckoutQuery,
    LabeledPrice,
    ReplyKeyboardMarkup,
)
from supabase import Client, create_client

from config import settings
from payments import payments_router
from admin import admin_router

# ─────────────────────────────────────────────
# Логирование
# ─────────────────────────────────────────────
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# Константы
# ─────────────────────────────────────────────
ADMIN_ID = 8701563086

TRIGGERS = {
    "fear":      "😱 Страх",
    "curiosity": "🤔 Любопытство",
    "expertise": "🎓 Экспертность",
    "shock":     "🤯 Шок",
    "urgency":   "⚡ Срочность",
}

# Английские описания стилей для Flux-промпта (по dop.md)
STYLE_MAP = {
    "fear":      "fear, threat, danger, high tension, shocking YouTube thumbnail, desperate emotions",
    "curiosity": "curiosity, intrigue, mystery, high curiosity gap, unexpected discovery",
    "expertise": "expert authority, trust, professional, clean minimalistic YouTube thumbnail",
    "shock":     "shock, disbelief, jaw-dropping reveal, extreme surprise, viral content",
    "urgency":   "urgency, time pressure, last chance, critical moment, breaking news style",
}

# Позиция текста — чтобы не перекрывал глаза/рот
TEXT_POSITIONS = {
    "fear":      "on the left side",
    "curiosity": "at the bottom",
    "expertise": "on the right side",
    "shock":     "on the left side",
    "urgency":   "at the top",
}

# Эмоция на лице по тригеру
EMOTION_MAP = {
    "fear":      "shocked and scared facial expression, wide open eyes, dramatic lighting",
    "curiosity": "surprised and intrigued expression, raised eyebrow, slight smile",
    "expertise": "confident and calm expression, slight smile, direct eye contact",
    "shock":     "jaw-dropping expression, mouth open, eyes wide, disbelief",
    "urgency":   "urgent and serious expression, intense focus, determined look",
}

# Негативный промпт — запрет искажений лица и текста поверх лица
NEGATIVE_PROMPT = (
    "ugly, distorted face, bad anatomy, extra limbs, extra fingers, blurry, low resolution, "
    "text over the face, text covering the eyes, text covering the mouth, "
    "tiny text, unreadable text, random letters, watermark, logo, "
    "multiple faces, deformed face, face swap, morphed face, bad likeness, "
    "different person, changed identity"
)

FORMATS = {
    "16:9": "📺 Обычное видео",
    "9:16": "📱 Shorts",
}

# Шрифты с поддержкой кириллицы
CYRILLIC_FONTS = [
    "C:/Windows/Fonts/impact.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "C:/Windows/Fonts/calibrib.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]

# ─────────────────────────────────────────────
# Supabase
# ─────────────────────────────────────────────
supabase: Client = create_client(settings.supabase_url, settings.supabase_key)


# ─────────────────────────────────────────────
# Шрифт
# ─────────────────────────────────────────────
def get_font(size: int) -> ImageFont.FreeTypeFont:
    for path in CYRILLIC_FONTS:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()


# ─────────────────────────────────────────────
# FSM — состояния
# ─────────────────────────────────────────────
class BotStates(StatesGroup):
    waiting_links       = State()   # ссылки на канал
    choosing_format     = State()   # Shorts или обычное видео
    waiting_photo       = State()   # фото лица (опционально)
    waiting_video_topic = State()   # тема нового видео
    choosing_trigger    = State()   # эмоциональный тригер
    choosing_text       = State()   # выбор из 3 вариантов текста
    waiting_custom_text = State()   # ввод своего текста


# ─────────────────────────────────────────────
# YouTube helpers
# ─────────────────────────────────────────────
def extract_video_id(url: str) -> Optional[str]:
    for pattern in [
        r"(?:youtube\.com/watch\?v=|youtu\.be/)([A-Za-z0-9_-]{11})",
        r"youtube\.com/shorts/([A-Za-z0-9_-]{11})",
    ]:
        m = re.search(pattern, url)
        if m:
            return m.group(1)
    return None


def get_video_meta(video_id: str) -> dict:
    try:
        yt = build("youtube", "v3", developerKey=settings.youtube_api_key, cache_discovery=False)
        items = yt.videos().list(part="snippet", id=video_id).execute().get("items", [])
        if items:
            s = items[0]["snippet"]
            return {"title": s.get("title", ""), "channel": s.get("channelTitle", "")}
    except Exception as e:
        logger.warning(f"YouTube API ошибка {video_id}: {e}")
    return {"title": "", "channel": ""}


# ─────────────────────────────────────────────
# OpenRouter helpers
# ─────────────────────────────────────────────
async def openrouter_request(model: str, messages: list, max_tokens: int = 1024) -> str:
    async with httpx.AsyncClient(timeout=90) as client:
        resp = await client.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {settings.openrouter_api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://oblojka-ai-bot.ru",
                "X-Title": "Oblojka AI Bot",
            },
            json={"model": model, "messages": messages, "max_tokens": max_tokens},
        )
        if not resp.is_success:
            logger.error(f"OpenRouter [{resp.status_code}] {model}: {resp.text[:500]}")
            resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]


async def analyze_style(thumbnails_info: list[dict]) -> tuple[str, str, str]:
    """
    Анализирует стиль обложек через Claude 3.5 Sonnet.
    Возвращает (краткое_для_пользователя, полный_анализ_для_Claude, имя_канала).
    """
    channel_name = thumbnails_info[0]["channel"] if thumbnails_info else "Канал"
    titles_text = "\n".join(f"- «{t['title']}» | {t['channel']}" for t in thumbnails_info)

    raw_resp = await openrouter_request(
        "anthropic/claude-3.5-sonnet",
        [{"role": "user", "content": (
            f"Ты — эксперт по дизайну YouTube-обложек. Проанализируй названия видео с канала «{channel_name}»:\n{titles_text}\n\n"
            "Выведи ответ в формате JSON:\n"
            "{\n"
            '  "brief": "📺 Канал: name\\n📌 Тема: topic\\n🎨 Стиль: style (в 3 строчки)",\n'
            '  "full_analysis": "Детальное описание (цветовая палитра, типографика, композиция, эмоции) на 5-10 предложений."\n'
            "}"
        )}],
        max_tokens=900,
    )

    brief = f"📺 Канал: {channel_name}\n📌 Тема: Разное\n🎨 Стиль: Уникальный"
    full_analysis = raw_resp
    
    m = re.search(r"\{.*\}", raw_resp, re.DOTALL)
    if m:
        try:
            data = json.loads(m.group())
            brief = data.get("brief", brief)
            full_analysis = data.get("full_analysis", full_analysis)
        except Exception:
            pass

    return brief.strip(), full_analysis, channel_name


async def generate_text_variants(
    style_analysis: str,
    trigger: str,
    video_topic: str,
) -> list[str]:
    """
    Claude генерирует 3 варианта текста для обложки.
    Короткие (3-6 слов), цепляющие, кликабельные.
    """
    trigger_name = TRIGGERS.get(trigger, trigger)
    raw = await openrouter_request(
        "anthropic/claude-3.5-sonnet",
        [{"role": "user", "content": (
            "Ты — эксперт по YouTube-заголовкам.\n\n"
            f"Тема видео: «{video_topic}»\n"
            f"Тригер: {trigger_name}\n"
            f"Стиль канала: {style_analysis[:400]}\n\n"
            "Придумай 3 РАЗНЫХ варианта текста для обложки YouTube.\n"
            "Требования:\n"
            "- Максимум 5-6 слов каждый\n"
            "- На русском языке\n"
            "- Цепляющие, интригующие, кликабельные\n"
            "- Хорошо читаются крупным шрифтом на обложке\n"
            "- Каждый вариант передаёт тригер по-своему\n"
            "- СТРОГО БЕЗ СМАЙЛИКОВ И ЭМОДЗИ (никаких 🤯, 🔥 и т.д.)\n\n"
            "Ответ СТРОГО в JSON: {\"variants\": [\"текст1\", \"текст2\", \"текст3\"]}"
        )}],
        max_tokens=250,
    )

    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if m:
        try:
            data = json.loads(m.group())
            variants = data.get("variants", [])
            if len(variants) >= 3:
                # Очистка возможных эмодзи: оставляем только буквы, цифры и базовую пунктуацию
                cleaned = []
                for v in variants[:3]:
                    v_clean = re.sub(r'[^\w\s.,!?:-]', '', str(v)).strip()
                    cleaned.append(v_clean)
                return cleaned
        except Exception:
            pass
    # Fallback если Claude не ответил JSON
    return [f"Смотри: {video_topic[:18]}!", "Ты не ожидал этого...", "Срочно посмотри!"]


async def generate_strategy_and_prompt(
    style_analysis: str,
    channel_name: str,
    trigger: str,
    cover_text: str,
    video_topic: str,
    aspect_ratio: str,
    has_face: bool,
) -> dict:
    """
    Строит гипер-конверсионный персонализированный image-промпт, извлекая «ДНК стиля».
    ИИ (Gemini Image/Nano Banana) нарисует его.
    """
    is_shorts   = aspect_ratio == "9:16"
    aspect_desc = "vertical 9:16 YouTube Shorts" if is_shorts else "horizontal 16:9 YouTube thumbnail"
    emotion_desc = EMOTION_MAP.get(trigger, "surprised and intrigued expression")

    # Claude генерирует визуальную ДНК с учетом анализа канала
    raw_meta = await openrouter_request(
        "anthropic/claude-3.5-sonnet",
        [{"role": "user", "content": (
            f"Channel Style Analysis for {channel_name}:\n{style_analysis}\n\n"
            f"Video topic (in Russian): '{video_topic}'\n\n"
            "We are creating a hyper-viral, high-CTR YouTube thumbnail that matches this channel's unique aesthetic DNA, rather than generic MrBeast style.\n"
            "Reply in JSON only (no extra text) with:\n"
            "{\n"
            '  "strategy_ru": "1-2 sentence visual strategy in Russian explaining the approach",\n'
            '  "aesthetic_genre": "e.g., Minimalist Tech, Dark Cinematic Horror, Bright Finance, Cozy Cooking",\n'
            '  "color_palette": "Specific colors that match the analysis (e.g., Deep blues, vibrant teals)",\n'
            '  "background_setting": "A visually striking background setting related to the topic, in the channel style",\n'
            '  "lighting_style": "e.g., moody neon, bright natural, harsh studio",\n'
            '  "unique_hook": "1-2 highly viral elements specific to this video topic (e.g., floating glowing Bitcoin, cracked glass, smoke)",\n'
            '  "typography_style": "Describe the exact font style, color, shadows, and outline matching the channel\'s thumbnails typography"\n'
            "}"
        )}],
        max_tokens=650,
    )
    meta = {
        "strategy_ru": "Генерация персонализированного дизайна.",
        "aesthetic_genre": "Bright High-CTR",
        "color_palette": "High contrast colors",
        "background_setting": "Dynamic abstract background",
        "lighting_style": "Cinematic lighting",
        "unique_hook": "spark effects and floating particles",
        "typography_style": "Bold thick sans-serif font"
    }
    m = re.search(r"\{.*\}", raw_meta, re.DOTALL)
    if m:
        try:
            meta = {**meta, **json.loads(m.group())}
        except Exception:
            pass

    # Строим персонализированный промпт без жестких "MrBeast" клише
    image_prompt = (
        f"Create a highly clickable {aspect_desc} in a {meta['aesthetic_genre']} style. "
    )
            
    if has_face:
        image_prompt += (
            f"Use a person as the main subject. Make the person's face large, expressive, and highly recognizable, "
            f"with a {emotion_desc} to grab attention. "
        )
        
    image_prompt += (
        f"Background Setting: {meta['background_setting']}. "
        f"Lighting: {meta['lighting_style']}. "
        f"Color Palette: {meta['color_palette']}. "
        f"Incorporate these unique visual hooks: {meta['unique_hook']}. "
        "Keep the composition clean, bold, and incredibly eye-catching on small mobile screens. "
        "The overall aesthetic must reflect the requested genre perfectly while being highly viral. "
        f"The thumbnail MUST reflect the signature visual style of the '{channel_name}' channel. "
    )

    image_prompt += "CRITICAL: Do NOT add any fake or garbled text, random UI elements, or strange symbols anywhere in the background. If there are app interfaces or secondary thumbnails in the background, keep them completely blank without any letters or numbers. "

    if cover_text:
        image_prompt += (
            f'On the image, flawlessly draw massive typography spelling EXACTLY this phrase: "{cover_text}". '
            f'Style the typography according to this description: {meta.get("typography_style", "Bold thick sans-serif")}. (Note: Use this description only for styling, DO NOT write these style words literally on the image!). '
            "Make sure the text is crisp, prominent, and stands out powerfully. "
            f'CRITICAL: The ONLY readable text on the ENTIRE image must be "{cover_text}". Absolutely NO other words, NO font names, NO extra letters, NO random numbers, NO garbled text. '
        )
        if is_shorts:
            image_prompt += "For this vertical Shorts design, place the typography tightly in one block at the top or center, avoiding scattered text in the empty space. "

    return {"strategy": meta.get("strategy_ru", "Создание обложки..."), "image_prompt": image_prompt}


# Стандартные размеры YouTube
YT_SIZES = {"16:9": (1280, 720), "9:16": (720, 1280)}

# ─────────────────────────────────────────────
# Replicate — генерация фона (без лица)
# ─────────────────────────────────────────────
async def generate_image_replicate(
    prompt: str,
    aspect_ratio: str = "16:9",
    face_bytes: Optional[bytes] = None,
    trigger: str = "curiosity",
    custom_text: Optional[str] = None
) -> Optional[bytes]:
    """
    Генерирует кинематографичный фон И ВШИВАЕТ РУССКИЙ ТЕКСТ через промпт.
    Оставляет пустое место для лица.
    """
    try:
        import replicate as _replicate
        client = _replicate.Client(api_token=settings.replicate_api_token)

        bg_prompt = prompt

        tmp_path = None
        output = None
        
        if face_bytes:
            import tempfile
            with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
                f.write(face_bytes)
                tmp_path = f.name
            
            w, h = (1280, 720) if aspect_ratio == "16:9" else (720, 1280)
            with open(tmp_path, "rb") as image_file:
                output = await asyncio.to_thread(
                    client.run,
                    "zsxkib/flux-pulid",
                    input={
                        "prompt": bg_prompt,
                        "negative_prompt": "No letterboxing, no borders, no black bars.",
                        "num_outputs": 1,
                        "width": w,
                        "height": h,
                        "output_format": "jpg",
                        "output_quality": 95,
                        "num_steps": 20,
                        "guidance_scale": 4.0,
                        "id_weight": 1.0,
                        "main_face_image": image_file
                    },
                )
        else:
            output = await asyncio.to_thread(
                client.run,
                "black-forest-labs/flux-dev",
                input={
                    "prompt": bg_prompt,
                    "negative_prompt": "No letterboxing, no borders, no black bars.",
                    "num_outputs": 1,
                    "aspect_ratio": aspect_ratio,
                    "output_format": "jpg",
                    "output_quality": 95,
                    "num_inference_steps": 28,
                    "guidance": 3.5,
                },
            )

        if tmp_path:
            try:
                import os
                os.remove(tmp_path)
            except Exception:
                pass

        if output:
            if isinstance(output, list):
                image_url = str(output[0])
            else:
                image_url = str(output)
                
            async with httpx.AsyncClient(timeout=60) as http:
                resp = await http.get(image_url)
                if resp.status_code == 200:
                    return resp.content

    except Exception as e:
        logger.error(f"Replicate ошибка генерации: {e}")
    return None

async def generate_image_openrouter(
    bg_prompt: str,
    aspect_ratio: str,
    face_bytes: bytes | None,
    trigger: str,
    custom_text: str | None = None,
) -> bytes | None:
    """Генерация изображения через OpenRouter."""
    import base64
    content = [{"type": "text", "text": bg_prompt}]
    if face_bytes:
        b64 = base64.b64encode(face_bytes).decode("utf-8")
        content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})

    try:
        async with httpx.AsyncClient(timeout=120) as http:
            resp = await http.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {settings.openrouter_api_key}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://oblojka-ai-bot.ru",
                    "X-Title": "Oblojka AI Bot",
                },
                json={
                    "model": "google/gemini-3.1-flash-image-preview",
                    "messages": [{"role": "user", "content": content}],
                },
            )
            
        data = resp.json()
        
        # Ошибка API
        if not resp.is_success:
            logger.error(f"OpenRouter Image Gen Error: {resp.text[:500]}")
            return None
            
        # OpenRouter возвращает картинки в массиве images
        message = data.get("choices", [{}])[0].get("message", {})
        images = message.get("images", [])
        
        if images and "image_url" in images[0]:
            b64_url = images[0]["image_url"].get("url", "")
            if b64_url.startswith("data:image"):
                b64_data = b64_url.split(",", 1)[-1]
                return base64.b64decode(b64_data)
        
        # Резервный поиск URL в контенте, если вдруг модель вернула MD
        content_str = message.get("content")
        if content_str:
            m = re.search(r'(https?://[^\s\)",\]\']+)', content_str)
            if m:
                image_url = m.group(1)
                async with httpx.AsyncClient(timeout=60) as dl:
                    img_resp = await dl.get(image_url)
                    if img_resp.status_code == 200:
                        return img_resp.content
                        
        logger.error(f"OpenRouter не вернул картинку. Ответ: {json.dumps(data)[:500]}")
        return None
        
    except Exception as e:
        logger.error(f"OpenRouter ошибка генерации изображения: {e}")
        return None


def composite_cutout_face(
    bg_bytes: bytes,
    face_bytes: bytes,
    trigger: str,
    aspect_ratio: str,
) -> bytes:
    """
    1. Вырезает фон с фото пользователя через rembg.
    2. Размещает вырезанный силуэт на сгенерированном фоне.
    """
    try:
        from rembg import remove
        
        # 1. Удаляем фон
        try:
            face_cutout_bytes = remove(face_bytes)
            face_img = Image.open(io.BytesIO(face_cutout_bytes)).convert("RGBA")
            # Обрезаем прозрачные края
            bbox = face_img.getbbox()
            if bbox:
                face_img = face_img.crop(bbox)
        except Exception as e:
            logger.error(f"rembg error: {e}")
            face_img = Image.open(io.BytesIO(face_bytes)).convert("RGBA")
        
        tw, th = YT_SIZES.get(aspect_ratio, (1280, 720))
        bg = Image.open(io.BytesIO(bg_bytes)).convert("RGBA").resize((tw, th), Image.LANCZOS)
        
        face_on_right = trigger in {"fear", "shock", "curiosity"}
        is_vertical = aspect_ratio == "9:16"

        if is_vertical:
            # Shorts: портрет снизу-посередине
            fw = int(tw * 0.85)
            # Считаем высоту пропорционально
            fi_w, fi_h = face_img.size
            if fi_w == 0 or fi_h == 0:
                return bg_bytes
            fh = int((fw / fi_w) * fi_h)
            face_resized = face_img.resize((fw, fh), Image.LANCZOS)
            
            x_pos = (tw - fw) // 2
            y_pos = th - fh
            bg.paste(face_resized, (x_pos, y_pos), face_resized)
        else:
            # YouTube: портрет на одной стороне (низ выравнивается по низу обложки)
            fw = int(tw * 0.45)
            fi_w, fi_h = face_img.size
            if fi_w == 0 or fi_h == 0:
                return bg_bytes
            fh = int((fw / fi_w) * fi_h)
            
            # Если высота лица больше высоты холста (очень длинное фото) — масштабируем по высоте
            if fh > int(th * 0.95):
                fh = int(th * 0.95)
                fw = int((fh / fi_h) * fi_w)
                
            face_resized = face_img.resize((fw, fh), Image.LANCZOS)
            
            x_pos = tw - fw if face_on_right else 0
            y_pos = th - fh
            bg.paste(face_resized, (x_pos, y_pos), face_resized)

        buf = io.BytesIO()
        bg.convert("RGB").save(buf, format="JPEG", quality=95)
        return buf.getvalue()

    except Exception as e:
        logger.error(f"Ошибка вырезки лица: {e}")
        return bg_bytes



# ─────────────────────────────────────────────
# PIL — текстовый оверлей
# ─────────────────────────────────────────────
# Цвета текста на обложке по стилю тригера
TEXT_COLORS = {
    "fear":      (255, 50,  50,  255),  # красный
    "curiosity": (255, 230, 30,  255),  # жёлтый
    "expertise": (255, 255, 255, 255),  # белый
    "shock":     (255, 100, 0,   255),  # оранжевый
    "urgency":   (255, 30,  30,  255),  # ярко-красный
}


def add_text_overlay(
    image_bytes: bytes,
    text: str,
    aspect_ratio: str = "16:9",
    trigger: str = "curiosity",
) -> bytes:
    """
    Добавляет русский текст поверх изображения.
    - Размер адаптируется под 16:9 и 9:16
    - Цвет соответствует тригеру (стиль канала)
    - Текст НЕ перекрывает лицо (позиция по тригеру)
    """
    if not text:
        return image_bytes

    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
        w, h = img.size

        # Убеждаемся в правильных размерах
        tw, th = YT_SIZES.get(aspect_ratio, (1280, 720))
        if (w, h) != (tw, th):
            img = img.resize((tw, th), Image.LANCZOS)
            w, h = tw, th

        is_vertical = aspect_ratio == "9:16"
        font_size = max(int(h * 0.082) if is_vertical else int(h * 0.105), 52)
        font = get_font(font_size)

        text_color = TEXT_COLORS.get(trigger, (255, 230, 30, 255))

        # Перенос слов
        dummy = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
        # Для 16:9 с лицом: текст занимает только половину ширины
        face_on_right = trigger in {"fear", "shock", "curiosity"}
        if is_vertical:
            max_w = int(w * 0.88)
            x_offset = int(w * 0.06)
        else:
            # Лицо занимает одну сторону, текст — другую
            max_w = int(w * 0.44)
            x_offset = int(w * 0.04) if not face_on_right else int(w * 0.04)

        words = text.split()
        lines: list[str] = []
        cur = ""
        for word in words:
            test = f"{cur} {word}".strip()
            bb = dummy.textbbox((0, 0), test, font=font)
            if bb[2] - bb[0] <= max_w:
                cur = test
            else:
                if cur:
                    lines.append(cur)
                cur = word
        if cur:
            lines.append(cur)

        line_h = int(font_size * 1.15)
        pad_v = int(h * 0.04)

        if is_vertical:
            # Для Shorts — текст выше лица (в верхней половине)
            block_h = len(lines) * line_h
            y_start = int(h * 0.15)
        else:
            # Для 16:9 — текст вертикально по центру на нужной стороне
            block_h = len(lines) * line_h
            y_start = (h - block_h) // 2

        draw = ImageDraw.Draw(img)
        y_cur = y_start

        for line in lines:
            bb = draw.textbbox((0, 0), line, font=font)
            lw = bb[2] - bb[0]

            if is_vertical:
                x_cur = (w - lw) // 2
            elif face_on_right:
                # Лицо справа → текст слева
                x_cur = x_offset
            else:
                # Лицо слева → текст справа
                x_cur = int(w * 0.50) + x_offset + ((max_w - lw) // 2)

            # Тень
            draw.text((x_cur + 8, y_cur + 8), line, font=font, fill=(0, 0, 0, 180))
            # Мощный контур 4px
            for dx in [-4, -2, 0, 2, 4]:
                for dy in [-4, -2, 0, 2, 4]:
                    draw.text((x_cur+dx, y_cur+dy), line, font=font, fill=(0,0,0,255))
            # Основной текст
            draw.text((x_cur, y_cur), line, font=font, fill=text_color)

            y_cur += line_h

        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=95)
        return buf.getvalue()

    except Exception as e:
        logger.error(f"Ошибка текстового оверлея: {e}")
        return image_bytes


# ─────────────────────────────────────────────
# Supabase — пользователи
# ─────────────────────────────────────────────
WELCOME_TEXT = (
    "🎨 <b>Обложка AI — Твой персональный дизайнер</b>\n\n"
    "✅ Анализ ДНК стиля вашего канала\n"
    "✅ 3 AI-варианта текста для обложки\n"
    "✅ Поддержка 16:9 и Shorts 9:16\n"
    "✅ Генерация за 30 секунд\n"
    "✅ Бонусы за приглашение друзей\n\n"
    "Нажмите <b>🎨 Создать обложку</b>, чтобы начать!"
)



def get_or_create_user(user_id: int, username: str = "", referrer_id: int | None = None) -> dict:
    res = supabase.table("users").select("*").eq("user_id", user_id).execute()
    if res.data:
        return res.data[0]
    new_user = {
        "user_id": user_id, 
        "username": username or "", 
        "generations_used": 0, 
        "balance": 1
    }
    if referrer_id and referrer_id != user_id:
        new_user["referrer_id"] = referrer_id
        
    created = supabase.table("users").insert(new_user).execute()
    return created.data[0] if created.data else new_user


def can_generate(user_id: int) -> bool:
    if user_id == ADMIN_ID:
        return True
    return get_or_create_user(user_id).get("balance", 0) > 0


def increment_generations(user_id: int) -> None:
    if user_id == ADMIN_ID:
        return
    user = get_or_create_user(user_id)
    new_balance = max(0, user.get("balance", 0) - 1)
    supabase.table("users").update(
        {
            "generations_used": user.get("generations_used", 0) + 1,
            "balance": new_balance
        }
    ).eq("user_id", user_id).execute()


def get_generations_left(user_id: int) -> int:
    if user_id == ADMIN_ID:
        return 999
    return get_or_create_user(user_id).get("balance", 0)


# ─────────────────────────────────────────────
# Клавиатуры
# ─────────────────────────────────────────────
def kb_main(balance: int = 0) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🎨 Создать обложку")],
            [KeyboardButton(text="🖼 Мои работы"), KeyboardButton(text="👥 Бонусы")],
            [KeyboardButton(text="👤 Мой профиль"), KeyboardButton(text=f"💵 Баланс: {balance}")],
        ],
        resize_keyboard=True,
    )


def kb_back_inline() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_main")],
    ])


def kb_format() -> InlineKeyboardMarkup:
    """Выбор формата: обычное видео 16:9 или Shorts 9:16."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📺 Обычное видео (16:9)", callback_data="format:16:9")],
        [InlineKeyboardButton(text="📱 Shorts (9:16)", callback_data="format:9:16")],
    ])


def kb_photo_choice() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📷 Загрузить своё фото", callback_data="photo:upload")],
        [InlineKeyboardButton(text="⏭ Без фото", callback_data="photo:skip")],
    ])


def kb_triggers() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=f"trigger:{key}")]
        for key, label in TRIGGERS.items()
    ])


def kb_text_variants(variants: list[str]) -> InlineKeyboardMarkup:
    """Клавиатура с 3 вариантами текста + своя надпись."""
    buttons = [
        [InlineKeyboardButton(text=f"➤ {v}", callback_data=f"variant:{i}")]
        for i, v in enumerate(variants)
    ]
    buttons.append([InlineKeyboardButton(text="✏️ Свой текст", callback_data="text:custom")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def kb_back(show_change_text: bool = False) -> InlineKeyboardMarkup:
    buttons = []
    if show_change_text:
        buttons.append([InlineKeyboardButton(text="📝 Изменить текст", callback_data="change_text")])
    buttons.append([InlineKeyboardButton(text="🔄 Сначала", callback_data="restart")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ─────────────────────────────────────────────
# Роутер и обработчики
# ─────────────────────────────────────────────
router = Router()


async def send_main_menu(message: Message, user_id: int):
    """Отправляет главное меню с ReplyKeyboard."""
    left = get_generations_left(user_id)
    await message.answer(WELCOME_TEXT, parse_mode=ParseMode.HTML, reply_markup=kb_main(left))


@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject, state: FSMContext) -> None:
    await state.clear()

    referrer_id = None
    if command.args:
        try:
            referrer_id = int(command.args)
        except ValueError:
            pass

    get_or_create_user(message.from_user.id, message.from_user.username or "", referrer_id=referrer_id)
    await send_main_menu(message, message.from_user.id)


# ── Reply-кнопка: 🎨 Создать обложку ──
@router.message(F.text == "🎨 Создать обложку")
async def btn_create_cover(message: Message, state: FSMContext) -> None:
    await state.clear()
    if not can_generate(message.from_user.id):
        from payments import kb_tariffs
        await message.answer(
            "😢 <b>У вас закончились генерации!</b>\n\nПополните баланс:",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_tariffs(),
        )
        return
    await state.set_state(BotStates.waiting_links)
    await message.answer(
        "🔗 <b>Отправьте 5–10 ссылок</b> на YouTube-видео или Shorts вашего канала "
        "(каждая с новой строки):",
        parse_mode=ParseMode.HTML,
    )


# ── Reply-кнопка: 🖼 Мои работы ──
@router.message(F.text == "🖼 Мои работы")
async def btn_my_works(message: Message) -> None:
    res = (
        supabase.table("user_history")
        .select("*")
        .eq("user_id", message.from_user.id)
        .order("created_at", desc=True)
        .limit(5)
        .execute()
    )
    if not res.data:
        await message.answer(
            "🖼 <b>Мои работы</b>\n\nУ вас пока нет сохранённых обложек.",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_back_inline(),
        )
        return
    await message.answer("🖼 <b>Ваши последние обложки:</b>", parse_mode=ParseMode.HTML)
    for record in reversed(res.data):
        text = record.get("prompt") or "Без текста"
        try:
            await message.answer_document(document=record["file_id"], caption=f"💬 {text}")
        except Exception:
            pass
    await message.answer("⬇️", reply_markup=kb_back_inline())


# ── Reply-кнопка: 👥 Бонусы ──
@router.message(F.text == "👥 Бонусы")
async def btn_bonuses(message: Message) -> None:
    bot_info = await message.bot.me()
    ref_link = f"https://t.me/{bot_info.username}?start={message.from_user.id}"
    await message.answer(
        "👥 <b>Бонусная программа</b>\n\n"
        "🎁 Приглашайте друзей и получайте бонусы!\n\n"
        "📌 <b>Как это работает:</b>\n"
        "1️⃣ Отправьте другу вашу ссылку\n"
        "2️⃣ Друг регистрируется в боте\n"
        "3️⃣ После его первой покупки вы получаете <b>+3 генерации</b>\n\n"
        f"🔗 <b>Ваша ссылка:</b>\n<code>{ref_link}</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=kb_back_inline(),
    )


# ── Reply-кнопка: 👤 Мой профиль ──
@router.message(F.text == "👤 Мой профиль")
async def btn_profile(message: Message) -> None:
    user = get_or_create_user(message.from_user.id, message.from_user.username or "")
    balance = user.get("balance", 0)
    used = user.get("generations_used", 0)
    created = str(user.get("created_at", ""))[:10]
    await message.answer(
        f"👤 <b>Мой профиль</b>\n\n"
        f"🆔 ID: <code>{message.from_user.id}</code>\n"
        f"💰 Баланс: <b>{balance}</b> генераций\n"
        f"🎨 Использовано: {used}\n"
        f"📅 Регистрация: {created}",
        parse_mode=ParseMode.HTML,
        reply_markup=kb_back_inline(),
    )


# ── Reply-кнопка: 💵 Баланс ──
@router.message(F.text.startswith("💵 Баланс"))
async def btn_balance(message: Message) -> None:
    from payments import kb_tariffs
    left = get_generations_left(message.from_user.id)
    await message.answer(
        f"💰 <b>Ваш баланс: {left} генераций</b>\n\n"
        "Выберите пакет для пополнения:",
        parse_mode=ParseMode.HTML,
        reply_markup=kb_tariffs(),
    )


# ── Callback: ⬅️ Назад → главное меню ──
@router.callback_query(F.data == "back_to_main")
async def cb_back_to_main(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    try:
        await callback.message.delete()
    except Exception:
        pass
    await send_main_menu(callback.message, callback.from_user.id)


# ── Ссылки ──
@router.message(BotStates.waiting_links)
async def handle_links(message: Message, state: FSMContext) -> None:
    lines = [l.strip() for l in (message.text or "").splitlines() if l.strip()]
    video_ids = [vid for l in lines if (vid := extract_video_id(l))]

    if len(video_ids) < 3:
        await message.answer("⚠️ Нужно минимум 3 корректных YouTube-ссылки. Попробуйте снова.")
        return

    video_ids = video_ids[:10]
    prog = await message.answer(f"⏳ Получаю данные для <b>{len(video_ids)} видео</b>...", parse_mode=ParseMode.HTML)

    thumbnails_info = [await asyncio.to_thread(get_video_meta, v) for v in video_ids]
    await prog.edit_text("🧠 Анализирую стиль обложек...")

    try:
        brief, full_analysis, channel_name = await analyze_style(thumbnails_info)
    except Exception as e:
        logger.error(f"Ошибка анализа: {e}")
        await prog.edit_text("❌ Не удалось проанализировать. Попробуйте другие ссылки.")
        return

    await state.update_data(style_analysis=full_analysis, channel_name=channel_name)
    await prog.edit_text(f"✅ <b>Стиль изучен!</b>\n\n{brief}", parse_mode=ParseMode.HTML)

    # Шаг 2: выбор формата
    await state.set_state(BotStates.choosing_format)
    await message.answer(
        "📐 <b>Выберите формат обложки:</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=kb_format(),
    )


# ── Выбор формата ──
@router.callback_query(F.data.startswith("format:"), BotStates.choosing_format)
async def cb_format(callback: CallbackQuery, state: FSMContext) -> None:
    ratio = callback.data.split("format:")[1]   # "16:9" или "9:16"
    await state.update_data(aspect_ratio=ratio)
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)

    format_name = FORMATS.get(ratio, ratio)
    await callback.message.answer(
        f"✅ Формат: <b>{format_name} ({ratio})</b>\n\n"
        "📷 <b>Хотите добавить своё фото?</b>\n"
        "Лицо будет встроено в дизайн. Фото используется только для этой генерации.",
        parse_mode=ParseMode.HTML,
        reply_markup=kb_photo_choice(),
    )
    await state.set_state(BotStates.waiting_photo)


# ── Фото: пропустить ──
@router.callback_query(F.data == "photo:skip", BotStates.waiting_photo)
async def cb_photo_skip(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    await state.update_data(face_bytes=None)
    await state.set_state(BotStates.waiting_video_topic)
    await callback.message.answer(
        "🎬 <b>Введите тему вашего нового видео</b> (название или краткое описание):",
        parse_mode=ParseMode.HTML,
    )


# ── Фото: загрузить ──
@router.callback_query(F.data == "photo:upload", BotStates.waiting_photo)
async def cb_photo_upload(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(
        "📸 Отправьте чёткую фотографию вашего лица.\n"
        "<i>Используется только для этой генерации.</i>",
        parse_mode=ParseMode.HTML,
    )


# ── Получаем фото ──
@router.message(BotStates.waiting_photo, F.photo | F.document)
async def handle_face_photo(message: Message, state: FSMContext) -> None:
    prog = await message.answer("⏳ Обрабатываю загруженный файл...")

    if message.photo:
        file_id = message.photo[-1].file_id
    elif message.document:
        if message.document.mime_type and not message.document.mime_type.startswith("image/"):
            await prog.edit_text("⚠️ Пожалуйста, отправьте картинку (JPEG, PNG).")
            return
        file_id = message.document.file_id
    else:
        await prog.edit_text("⚠️ Ожидаю фото или картинку-документ.")
        return

    file = await message.bot.get_file(file_id)
    buf = io.BytesIO()
    await message.bot.download_file(file.file_path, destination=buf)

    await state.update_data(face_bytes=buf.getvalue())
    await prog.edit_text("✅ <b>Фото готово!</b>", parse_mode=ParseMode.HTML)

    await state.set_state(BotStates.waiting_video_topic)
    await message.answer(
        "🎬 <b>Введите тему вашего нового видео</b> (название или краткое описание):",
        parse_mode=ParseMode.HTML,
    )


# ── Тема видео ──
@router.message(BotStates.waiting_video_topic)
async def handle_video_topic(message: Message, state: FSMContext) -> None:
    topic = (message.text or "").strip()
    if not topic:
        await message.answer("⚠️ Пожалуйста, введите тему видео.")
        return

    await state.update_data(video_topic=topic)
    await state.set_state(BotStates.choosing_trigger)
    await message.answer(
        f"✅ Тема: <b>«{topic}»</b>\n\n"
        "🎯 Выберите <b>эмоциональный тригер</b> для обложки:",
        parse_mode=ParseMode.HTML,
        reply_markup=kb_triggers(),
    )


# ── Выбор тригера → генерируем 3 варианта текста ──
@router.callback_query(F.data.startswith("trigger:"), BotStates.choosing_trigger)
async def cb_trigger(callback: CallbackQuery, state: FSMContext) -> None:
    key = callback.data.split(":")[1]
    await state.update_data(trigger=key)
    await callback.answer()

    data = await state.get_data()
    video_topic = data.get("video_topic", "")
    style_analysis = data.get("style_analysis", "")
    trigger_name = TRIGGERS.get(key, key)

    prog = await callback.message.edit_text(
        f"Тригер: <b>{trigger_name}</b> ✅\n\n✨ Генерирую варианты текста...",
        parse_mode=ParseMode.HTML,
    )

    try:
        variants = await generate_text_variants(style_analysis, key, video_topic)
    except Exception as e:
        logger.error(f"Ошибка генерации вариантов: {e}")
        variants = [f"{video_topic[:18]}!", "Не пропусти!", "Смотри сейчас"]

    await state.update_data(text_variants=variants)
    await state.set_state(BotStates.choosing_text)

    variants_text = "\n".join(f"  {i+1}. «{v}»" for i, v in enumerate(variants))
    await prog.edit_text(
        f"✨ <b>3 варианта текста для обложки:</b>\n\n{variants_text}\n\n"
        "Выберите вариант или введите свой:",
        parse_mode=ParseMode.HTML,
        reply_markup=kb_text_variants(variants),
    )


# ── Выбор варианта текста ──
@router.callback_query(F.data.startswith("variant:"), BotStates.choosing_text)
async def cb_select_variant(callback: CallbackQuery, state: FSMContext) -> None:
    idx = int(callback.data.split(":")[1])
    data = await state.get_data()
    variants = data.get("text_variants", [])
    chosen = variants[idx] if idx < len(variants) else ""

    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    await _generate_cover(callback.message, state, callback.from_user.id, custom_text=chosen)


# ── Свой текст ──
@router.callback_query(F.data == "text:custom", BotStates.choosing_text)
async def cb_text_custom(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(BotStates.waiting_custom_text)
    await callback.message.edit_text(
        "✏️ Введите текст для обложки <b>(на русском, 3-6 слов):</b>",
        parse_mode=ParseMode.HTML,
    )


@router.message(BotStates.waiting_custom_text)
async def handle_custom_text(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer("⚠️ Текст не может быть пустым.")
        return
    await _generate_cover(message, state, message.from_user.id, custom_text=text)


# ─────────────────────────────────────────────
# Основная генерация
# ─────────────────────────────────────────────
async def _generate_cover(
    message: Message,
    state: FSMContext,
    user_id: int,
    custom_text: str,
) -> None:
    """
    Полный цикл генерации:
    1. Claude → стратегия + image_prompt (с объектами из темы)
    2. Replicate → изображение (с лицом или без)
    3. PIL → русский текст оверлей
    4. Отправка
    """
    if not can_generate(user_id):
        from payments import kb_tariffs
        promo_text = (
            "😢 <b>Недостаточно генераций на балансе!</b>\n\n"
            "В отличие от обычных ботов с шаблонными картинками, мы используем глубокий анализ "
            "ДНК вашего канала.\n\n"
            "Пожалуйста, пополните баланс, чтобы продолжить:"
        )
        await message.answer(promo_text, parse_mode=ParseMode.HTML, reply_markup=kb_tariffs())
        await state.clear()
        return

    data = await state.get_data()
    style_analysis = data.get("style_analysis", "")
    channel_name   = data.get("channel_name", "Канал")
    trigger        = data.get("trigger", "curiosity")
    face_bytes     = data.get("face_bytes")
    video_topic    = data.get("video_topic", "")
    aspect_ratio   = data.get("aspect_ratio", "16:9")

    format_name = FORMATS.get(aspect_ratio, aspect_ratio)
    prog = await message.answer(
        f"⚙️ <b>Разрабатываю концепцию...</b> ({format_name})",
        parse_mode=ParseMode.HTML,
    )

    # Шаг 1: Claude → стратегия и промпт
    try:
        result = await generate_strategy_and_prompt(
            style_analysis, channel_name, trigger, custom_text, video_topic, aspect_ratio,
            has_face=bool(face_bytes),
        )
    except Exception as e:
        logger.error(f"Ошибка Claude: {e}")
        await prog.edit_text("❌ Ошибка при создании концепции. Попробуйте позже.")
        return

    strategy     = result.get("strategy", "")
    image_prompt = result.get("image_prompt", "")

    await prog.edit_text(
        f"🎨 <b>Концепция:</b> <i>{strategy}</i>\n\n🖼 Генерирую изображение...",
        parse_mode=ParseMode.HTML,
    )

    # Шаг 2: OpenRouter → кинематографичный фон + лицо + русский текст
    try:
        image_bytes = await generate_image_openrouter(
            image_prompt, aspect_ratio, face_bytes, trigger, custom_text
        )
    except Exception as e:
        logger.error(f"Ошибка Replicate: {e}")
        image_bytes = None

    if not image_bytes:
        await prog.edit_text("❌ Не удалось сгенерировать изображение. Попробуйте позже.")
        return

    # Шаг 3: Отправляем без сжатия (документом)
    increment_generations(user_id)
    left = get_generations_left(user_id)
    trigger_name = TRIGGERS.get(trigger, trigger)

    caption = (
        f"🎨 <b>Ваша обложка готова!</b>\n\n"
        f"📐 Формат: {format_name} ({aspect_ratio})\n"
        f"🎯 Тригер: {trigger_name}\n"
        f"💬 Текст: <i>{custom_text or '—'}</i>\n\n"
        f"📋 <b>Концепция:</b> {strategy[:250]}\n\n"
        f"🎁 Генераций осталось: <b>{'∞' if user_id == ADMIN_ID else left}</b>\n\n"
        f"⚠️ <b>Внимание:</b> Обязательно сохраните обложку к себе на устройство!"
    )

    await prog.delete()
    
    file_name = "thumbnail_16x9.jpg" if aspect_ratio == "16:9" else "shorts_9x16.jpg"

    sent_msg = await message.answer_document(
        document=BufferedInputFile(image_bytes, filename=file_name),
        caption=caption[:1024],
        parse_mode=ParseMode.HTML,
        reply_markup=kb_back(show_change_text=True),
    )
    
    # Сохраняем в таблицу user_history
    try:
        supabase.table("user_history").insert({
            "user_id": user_id, 
            "file_id": sent_msg.document.file_id, 
            "prompt": custom_text
        }).execute()
    except Exception as e:
        logger.error(f"Не удалось сохранить генерацию в БД: {e}")


@router.callback_query(F.data == "change_text")
async def cb_change_text(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    data = await state.get_data()
    variants = data.get("text_variants", [])
    if not variants:
        await callback.message.answer("⚠️ Нет сохранённых вариантов текста. Начните заново.")
        return

    await state.set_state(BotStates.choosing_text)
    variants_text = "\n".join(f"  {i+1}. «{v}»" for i, v in enumerate(variants))
    await callback.message.answer(
        f"✨ <b>Выберите другой вариант или введите свой:</b>\n\n{variants_text}",
        parse_mode=ParseMode.HTML,
        reply_markup=kb_text_variants(variants),
    )


@router.callback_query(F.data == "restart")
async def cb_restart(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        pass
    await send_main_menu(callback.message, callback.from_user.id)

# ─────────────────────────────────────────────
# Точка входа
# ─────────────────────────────────────────────
async def main() -> None:
    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    dp.include_router(payments_router)
    dp.include_router(admin_router)

    logger.info("🤖 Бот 'Обложка AI' запускается...")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
