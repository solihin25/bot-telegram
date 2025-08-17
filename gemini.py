import os
import io
import time
import queue
import requests
from threading import Thread, Event

from flask import Flask, request, jsonify
import telebot
from gtts import gTTS
import google.generativeai as genai
from dotenv import load_dotenv

# Muat file .env
load_dotenv()

# --- (1) KONFIGURASI KUNCI & BOT ---

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
else:
    print("[Peringatan] GEMINI_API_KEY belum diatur. Endpoint /translate-natural akan gagal.")

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    print("[Peringatan] BOT_TOKEN belum diatur. Bot Telegram tidak bisa berjalan tanpa token yang valid.")

ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "I7sakys8pBZ1Z5f0UhT9")  # default voice

# Inisialisasi ElevenLabs client
elevenlabs_client = None
try:
    from elevenlabs import ElevenLabs
    if ELEVENLABS_API_KEY:
        elevenlabs_client = ElevenLabs(api_key=ELEVENLABS_API_KEY)
        print("[Info] ElevenLabs client terinisialisasi.")
    else:
        print("[Info] ELEVENLABS_API_KEY tidak di-set. Akan pakai gTTS sebagai fallback.")
except Exception as e:
    print(f"[Peringatan] Gagal inisialisasi ElevenLabs SDK: {e}. Akan pakai gTTS sebagai fallback.")

# --- (2) KONFIGURASI BOT & ANTRIAN ---
DEFAULT_TARGET = "south"
TMP_DIR = "/data/data/com.termux/files/home" if os.path.exists("/data/data/com.termux") else "."
bot = telebot.TeleBot(BOT_TOKEN, parse_mode='HTML') if BOT_TOKEN else None

user_settings = {}
processing_queue = queue.Queue()
is_processing = Event()

# --- (3) UTILITY & TTS FALLBACK ---

def get_user_target(chat_id):
    return user_settings.get(chat_id, {}).get("target", DEFAULT_TARGET)

def set_user_target(chat_id, value):
    user_settings[chat_id] = {"target": value}

def make_tts_korean_bytes(korean_text: str) -> io.BytesIO:
    """Fallback TTS dengan gTTS (Bahasa Korea)."""
    tts = gTTS(text=korean_text, lang='ko')
    bio = io.BytesIO()
    tts.write_to_fp(bio)
    bio.seek(0)
    return bio

def get_elevenlabs_tts_bytes(text: str) -> io.BytesIO:
    """Mengambil audio TTS dari ElevenLabs."""
    if not elevenlabs_client:
        raise RuntimeError("ElevenLabs client belum tersedia.")

    stream = elevenlabs_client.text_to_speech.convert(
        voice_id=ELEVENLABS_VOICE_ID,
        model_id="eleven_multilingual_v2",
        text=text,
        output_format="mp3_44100_128"
    )

    bio = io.BytesIO()
    for chunk in stream:
        if isinstance(chunk, (bytes, bytearray)):
            bio.write(chunk)
    bio.seek(0)
    if bio.getbuffer().nbytes == 0:
        raise RuntimeError("Stream ElevenLabs kosong.")
    return bio

# --- (4) FLASK API UNTUK TERJEMAHAN (GEMINI) ---

app = Flask(__name__)

def translate_with_gemini(text_to_translate: str):
    """Panggil Gemini untuk menerjemahkan Indonesia -> Korea."""
    try:
        if not GEMINI_API_KEY:
            raise RuntimeError("GEMINI_API_KEY belum dikonfigurasi.")

        prompt_message = f"""
Kamu adalah penerjemah profesional yang ahli dalam Bahasa Indonesia dan Bahasa Korea.
Tugasmu adalah menerjemahkan teks Bahasa Indonesia ke Bahasa Korea yang natural.
Format output:
Teks Korea: [terjemahan_hangul]
Romanisasi: [terjemahan_romanisasi]

Teks Indonesia: "{text_to_translate}"
""".strip()

        model = genai.GenerativeModel("gemini-1.5-flash-latest")
        response = model.generate_content(prompt_message)
        raw_response_text = (getattr(response, "text", "") or "").strip()

        korean_text, romanization = "", ""
        for line in raw_response_text.splitlines():
            l = line.strip()
            if l.lower().startswith("teks korea:"):
                korean_text = l.split(":", 1)[1].strip()
            elif l.lower().startswith("romanisasi:"):
                romanization = l.split(":", 1)[1].strip()

        if not korean_text or not romanization:
            raise ValueError(f"Gagal parsing output Gemini: {raw_response_text}")

        return korean_text, romanization

    except Exception as e:
        print(f"[Error] translate_with_gemini: {e}")
        return None, None

@app.route('/translate-natural', methods=['POST'])
def translate_endpoint():
    data = request.get_json(silent=True) or {}
    if 'text' not in data or not isinstance(data['text'], str) or not data['text'].strip():
        return jsonify({"status": "error", "message": "Field 'text' wajib ada."}), 400

    text_to_translate = data['text'].strip()
    korean_text, romanization = translate_with_gemini(text_to_translate)

    if korean_text and romanization:
        return jsonify({
            "status": "success",
            "original_text": text_to_translate,
            "translated_korean": korean_text,
            "romanization": romanization
        }), 200

    return jsonify({"status": "error", "message": "Gagal hubungi Gemini API."}), 500

def start_flask_app():
    print("API penerjemah siap! http://127.0.0.1:5000")
    app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False, threaded=True)

# --- (5) TELEGRAM BOT ---

def start_telegram_bot():
    if bot is None:
        print("[Fatal] BOT_TOKEN belum valid.")
        return

    @bot.message_handler(commands=['start', 'help'])
    def send_welcome(message):
        text = (
            "Halo! Saya bot translate IND → KOR.\n\n"
            "Kirim pesan dalam Bahasa Indonesia, bot akan kirim terjemahan Korea."
        )
        bot.send_message(message.chat.id, text)

    @bot.message_handler(commands=['set'])
    def cmd_set(message):
        parts = (message.text or "").strip().split()
        if len(parts) < 2 or parts[1].lower() not in ("south", "north"):
            bot.reply_to(message, "Gunakan: /set south  atau /set north")
            return
        target = parts[1].lower()
        set_user_target(message.chat.id, target)
        bot.reply_to(message, f"Target set ke: {target}")

    @bot.message_handler(commands=['about'])
    def cmd_about(message):
        bot.send_message(message.chat.id,
            "Bot ini menerjemahkan ID → KO dengan Gemini + TTS ElevenLabs/gTTS.\n"
            "API lokal: http://127.0.0.1:5000/translate-natural"
        )

    @bot.message_handler(func=lambda m: True, content_types=['text'])
    def handle_all_text(message):
        if (message.text or "").strip():
            processing_queue.put(message)
            bot.send_message(message.chat.id, "✅ Pesan masuk antrian...")

    print("Bot Telegram berjalan...")
    bot.infinity_polling(timeout=60, long_polling_timeout=60, skip_pending=True)

# --- (6) WORKER ANTRIAN ---

def queue_worker():
    while True:
        message = processing_queue.get()
        chat_id = message.chat.id
        incoming = (message.text or "").strip()

        try:
            response = requests.post(
                "http://127.0.0.1:5000/translate-natural",
                json={"text": incoming},
                headers={"Content-Type": "application/json"},
                timeout=30
            )
            response.raise_for_status()
            data = response.json()
            if data.get("status") == "error":
                bot.send_message(chat_id, f"❌ Error API: {data.get('message')}")
                processing_queue.task_done()
                continue

            kor_text = data.get("translated_korean", "")
            pronunciation = data.get("romanization", "")

        except Exception as e:
            bot.send_message(chat_id, f"❌ Error terjemahan: {e}")
            processing_queue.task_done()
            continue

        # Generate TTS
        tts_bytes, performer = None, "TTS"
        try:
            if elevenlabs_client:
                tts_bytes = get_elevenlabs_tts_bytes(kor_text)
                performer = "ElevenLabs"
            else:
                raise RuntimeError("ElevenLabs tidak aktif.")
        except Exception as e:
            print(f"[Info] ElevenLabs gagal: {e}. Fallback gTTS.")
            try:
                tts_bytes = make_tts_korean_bytes(kor_text)
                performer = "gTTS"
            except Exception as e2:
                bot.send_message(chat_id, f"❌ Error TTS: {e2}")

        # Kirim ke Telegram
        reply_text = (
            f"📘 Terjemahan (ID → Korea) [{get_user_target(chat_id)}]\n\n"
            f"🔤 <b>Hangul:</b>\n{kor_text}\n\n"
            f"🔠 <b>Romanisasi:</b>\n{pronunciation}\n"
        )
        try:
            bot.send_message(chat_id, reply_text)
        except Exception as e:
            print(f"[Warning] Gagal kirim pesan: {e}")

        if tts_bytes:
            try:
                tts_bytes.seek(0)
                bot.send_audio(chat_id, tts_bytes, title="Pengucapan Korea", performer=performer)
            except Exception as e:
                bot.send_message(chat_id, f"❌ Gagal kirim audio: {e}")

        processing_queue.task_done()
        time.sleep(1.5)

# --- (7) MAIN ---

if __name__ == '__main__':
    Thread(target=start_flask_app, daemon=True).start()
    Thread(target=queue_worker, daemon=True).start()
    time.sleep(2.5)
    start_telegram_bot()
