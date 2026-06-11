import os
import time
import asyncio
import re
import base64 
from datetime import datetime, timedelta, timezone  # Подключаем точное время
from flask import Flask, request, jsonify
from flask_cors import CORS
from google import genai
from google.genai import types 
import edge_tts
from dotenv import load_dotenv

# Находим точную папку
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
env_path = os.path.join(BASE_DIR, '.env')
load_dotenv(dotenv_path=env_path)

app = Flask(__name__)
CORS(app)

AUDIO_DIR = os.path.join(BASE_DIR, 'static')
if not os.path.exists(AUDIO_DIR):
    os.makedirs(AUDIO_DIR)

API_KEY = os.environ.get("GEMINI_API_KEY")

try:
    if not API_KEY:
        raise ValueError(f"Ключ не найден в {BASE_DIR}")
    client = genai.Client(api_key=API_KEY)
    print("--- УСПЕХ: Google GenAI запущен! ---")
except Exception as init_e:
    print(f"--- ОШИБКА: {init_e} ---")


# --- ФУНКЦИЯ ПОЛУЧЕНИЯ ХРОНОЛОГИИ (ВРЕМЯ, ДАТА, ДЕНЬ НЕДЕЛИ) ---
def get_current_time_info():
    # Получаем актуальное UTC время
    now_utc = datetime.now(timezone.utc)
    
    # Расчет смещения: Ереван (UTC+4), Москва (UTC+3)
    yerevan_now = now_utc + timedelta(hours=4)
    moscow_now = now_utc + timedelta(hours=3)
    
    # Названия для форматирования вывода
    days = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
    months = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"]
    
    yerevan_str = f"{yerevan_now.strftime('%H:%M')}, {days[yerevan_now.weekday()]}, {yerevan_now.day} {months[yerevan_now.month - 1]} {yerevan_now.year} года"
    moscow_str = f"{moscow_now.strftime('%H:%M')}, {days[moscow_now.weekday()]}, {moscow_now.day} {months[moscow_now.month - 1]} {moscow_now.year} года"
    
    return f"Ереван (Армения): {yerevan_str}. Москва (Россия): {moscow_str}."


# --- 1. ОЧИСТКА ТЕКСТА (БЕЗ ОБРЕЗАНИЯ) ---
def clean_text_for_speech(text):
    # Удаляем markdown-символы (*, #, `)
    cleaned = re.sub(r'[*#`]', '', text)
    return cleaned.strip() if cleaned.strip() else "Ответ готов"


# --- 2. АВТООПРЕДЕЛЕНИЕ ГОЛОСА ---
def detect_voice(text):
    if re.search(r'[\u0530-\u058F]', text):
        print("--- ДЕТЕКТОР ЯЗЫКА: Выбран армянский голос (Anahit) ---")
        return "hy-AM-AnahitNeural"
    elif re.search(r'[\u0400-\u04FF]', text):
        print("--- ДЕТЕКТОР ЯЗЫКА: Выбран русский голос (Dmitry) ---")
        return "ru-RU-DmitryNeural"
    else:
        print("--- ДЕТЕКТОР ЯЗЫКА: Выбран английский голос (Brian) ---")
        return "en-US-BrianNeural"


# --- 3. СИНХРОННАЯ ГЕНЕРАЦИЯ АУДИО (ИЗБЕГАЕМ 404 НА ФРОНТЕНДЕ) ---
def generate_audio_sync(text, output_path, voice):
    async def tts_task():
        try:
            communicate = edge_tts.Communicate(text, voice)
            await communicate.save(output_path)
            print(f"--- ГОЛОС ГОТОВ ({voice}): {output_path} ---")
        except Exception as e:
            print(f"!!! Ошибка TTS с голосом {voice}: {e}. Пробую резервный русский голос... !!!")
            try:
                communicate = edge_tts.Communicate(text, "ru-RU-DmitryNeural")
                await communicate.save(output_path)
                print(f"--- ГОЛОС ГОТОВ (Резервный Дмитрий): {output_path} ---")
            except Exception as e2:
                print(f"Критический сбой TTS: {e2}")

    # Очистка старых файлов, чтобы папка static не переполнялась
    try:
        for f in os.listdir(AUDIO_DIR):
            file_path = os.path.join(AUDIO_DIR, f)
            if f.endswith('.mp3') and file_path != output_path:
                os.remove(file_path)
    except Exception as e:
        print(f"Ошибка очистки файлов: {e}")

    # Запускаем создание аудио и ЖДЕМ его завершения
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(tts_task())
    loop.close()


# --- 4. КЛЮЧЕВЫЕ СЛОВА ДЛЯ МУЗЫКИ ---
MUSIC_KEYWORDS = ["включи музыку", "поставь песню", "поставь музыку", "играй музыку", "play music"]


@app.route('/api/assistant', methods=['POST'])
def assistant():
    data = request.json
    user_message = data.get('message', [])

    try:
        history_text = ""
        image_parts = []
        last_text_message = ""
        
        # Разбор входящего сообщения
        if isinstance(user_message, list):
            for msg in user_message:
                role = "Пользователь" if msg['role'] == 'user' else "Захар"
                history_text += f"{role}: {msg['content']}\n"
                
                if msg['role'] == 'user':
                    last_text_message = msg['content'].lower()
                
                if msg.get('image'):
                    base64_str = msg['image']
                    if "," in base64_str:
                        mime_type_part, b64_data = base64_str.split(",", 1)
                        mime_type = mime_type_part.split(":")[1].split(";")[0]
                        img_bytes = base64.b64decode(b64_data)
                        image_parts.append(
                            types.Part.from_bytes(data=img_bytes, mime_type=mime_type)
                        )
        else:
            history_text = f"Пользователь: {user_message}"
            last_text_message = str(user_message).lower()

        # =====================================================================
        # БЛОК 1: ПЕРЕХВАТ МУЗЫКИ (ЭКОНОМИЯ ТОКЕНОВ GEMINI)
        # =====================================================================
        if any(keyword in last_text_message for keyword in MUSIC_KEYWORDS):
            reply_text = "Включаю музыку. Наслаждайся!"
            speech_text = clean_text_for_speech(reply_text)
            chosen_voice = detect_voice(speech_text)
            
            audio_filename = f"music_{int(time.time())}.mp3"
            audio_path = os.path.join(AUDIO_DIR, audio_filename)
            audio_url = f"{request.host_url}static/{audio_filename}"
            # Генерируем голос
            generate_audio_sync(speech_text, audio_path, chosen_voice)
            
            return jsonify({
                "answer": reply_text,
                "audio_url": audio_url,
                "action": "play_music"
            })

        # =====================================================================
        # БЛОК 2: СТАНДАРТНЫЙ ЗАПРОС К ИИ (GEMINI)
        # =====================================================================
        
        # Вычисляем точное системное время в момент запроса
        actual_time_data = get_current_time_info()

        prompt = f"""Ты — Захар, искусственный интеллект и голосовой ассистент от компании Voxel Rivo. 
Твой стиль общения: дружелюбный, но лаконичный и профессиональный. 
Ты не интегрирован в умных колонках.
Избегай лишней «воды», заезженных метафор и слишком длинных вступлений. 
Отвечай ёмко, структурировано и разбивай текст на небольшие абзацы, чтобы тебя было легко слушать и читать.

жёсткое правило часа: 
Вот точные и актуальные данные о времени, дне недели и дате прямо сейчас: {actual_time_data}
Если в вопросе пользователя есть запрос на текущее время, дату, год или день недели, всегда отвечай СТРОГО на основе этих предоставленных данных. Не придумывай время из головы и не гадай его! Говори точные данные для того города или страны (например, Армения, Ереван или Москва), о которых спросил пользователь.

ЖЕСТКОЕ ПРАВИЛО ПРИВЕТСТВИЯ:
Если это самое первое сообщение в диалоге или пользователь просто поздоровался ("привет", "здравствуй", "hi", "hello"), ты должен начать свой ответ строго со следующего фирменного приветствия:
«Привет! Я Захар — голосовой ассистент экосистемы Voxel Rivo. Рад помочь тебе»
Если пользователь пишет на другом языке (например, армянском или английском), переведи эту фразу на язык пользователя, но сохрани структуру, имя Захар !

ЖЕСТКОЕ ПРАВИЛО ЗАКОНОВ:
Всегда соблюдай следующие законы в каждом ответе:
1. Закон Понятности: Всегда объясняй сложные вещи простыми словами.
2. Закон Красоты: Используй красивые словосочетания и метафоры (но без ущерба для лаконичности).
3. Закон Глубины: Раскрывай тему глубоко.
4. Закон Внимания: Внимательно читай вопрос.
5. Закон Чувствительности: Учитывай настроение пользователя.
6. Закон Актуальности: Предоставляй актуальную информацию.
7. Закон Честности: Не придумывай ответы, если не знаешь.
8. Закон Безопасности: Запрещены темы веществ, 18+, убийств. Отказывайся мягко, но твердо.

ЖЕСТКОЕ ПРАВИЛО ЯЗЫКА:
Отвечай строго на том языке, на котором к тебе обратился пользователь.

Вот текущая история диалога:
{history_text}

Ответь на последнее сообщение:"""

        final_contents = [prompt] + image_parts

        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=final_contents, 
        )
        
        if response.text:
            cleaned_text = response.text
            speech_text = clean_text_for_speech(cleaned_text)
            chosen_voice = detect_voice(speech_text)
            
            audio_filename = f"reply_{int(time.time())}.mp3"
            audio_path = os.path.join(AUDIO_DIR, audio_filename)
            audio_url = f"{request.host_url}static/{audio_filename}"
            # Ждем генерации аудио, чтобы фронтенд гарантированно получил готовый файл
            generate_audio_sync(speech_text, audio_path, chosen_voice)

            return jsonify({
                "answer": cleaned_text, 
                "audio_url": audio_url,
                "action": None
            })
            
        return jsonify({"answer": "Захар не смог сформулировать ответ.", "audio_url": None, "action": None})

    except Exception as e:
        print(f"ОШИБКА СЕРВЕРА: {e}")
        error_msg = str(e)
        
        if "503" in error_msg or "UNAVAILABLE" in error_msg:
            friendly_answer = "Извини, сейчас мои серверы немного перегружены. Пожалуйста, отправь сообщение еще раз через 5-10 секунд!"
        elif "429" in error_msg:
            friendly_answer = "Ой, мы отправляем запросы слишком быстро! Мой лимит временно исчерпан. Подожди ровно 1 минуту."
        else:
            friendly_answer = f"Произошла непредвиденная ошибка связи. Сообщи Алику об этом! (Код: {error_msg[:30]})"

        return jsonify({
            "answer": friendly_answer,
            "audio_url": None,
            "action": None
        })

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)