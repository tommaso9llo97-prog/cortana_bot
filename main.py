import os
import sqlite3
import json
from datetime import datetime, timedelta
import requests
import dateparser
from fastapi import FastAPI, Request
import google.generativeai as genai
import random

# --- VARIABILI SEGRETE (le prende dal pannello di Render) ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# Controllo che le chiavi ci siano, altrimenti il server non parte
if not BOT_TOKEN or not GEMINI_API_KEY:
    raise Exception("Mancano le variabili BOT_TOKEN o GEMINI_API_KEY su Render!")

# Collegamento a Gemini (il cervello)
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-1.5-flash')

# Dove salvare il database (Render ci dà una cartella /data che non si cancella mai)
DB_PATH = "/data/cortana.db"
if not os.path.exists("/data"):
    DB_PATH = "cortana.db"  # Se sei in locale per test

app = FastAPI()

# --- GESTIONE DATABASE (la memoria di Cortana) ---
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    # Tabella principale dello stato (umore, castighi, alba)
    conn.execute('''
        CREATE TABLE IF NOT EXISTS user_state (
            id INTEGER PRIMARY KEY,
            user_id TEXT UNIQUE,
            chat_id TEXT,
            current_date TEXT,
            alba_done INTEGER DEFAULT 0,
            panic_count INTEGER DEFAULT 0,
            panic_block_until TEXT,
            last_city TEXT,
            last_checkin_time TEXT,
            mood_context TEXT DEFAULT 'neutro'
        )
    ''')
    # Tabella dei promemoria (per il Protocollo Chronos)
    conn.execute('''
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            task_text TEXT,
            due_date TEXT,
            done INTEGER DEFAULT 0
        )
    ''')
    # Inserisco il tuo profilo "default" se non esiste
    conn.execute(
        "INSERT OR IGNORE INTO user_state (user_id, current_date, mood_context) VALUES ('default', ?, 'neutro')",
        (datetime.now().strftime("%Y-%m-%d"),)
    )
    conn.commit()
    conn.close()

init_db()

# --- FUNZIONE PER INVIARE MESSAGGI SU TELEGRAM ---
def send_telegram_message(text):
    conn = get_db()
    row = conn.execute("SELECT chat_id FROM user_state WHERE user_id = 'default'").fetchone()
    conn.close()
    if not row or not row['chat_id']:
        return False  # Non so ancora chi sei, aspetto che scrivi prima
    chat_id = row['chat_id']
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"})
    return True

def save_chat_id(chat_id):
    conn = get_db()
    conn.execute("UPDATE user_state SET chat_id = ? WHERE user_id = 'default'", (str(chat_id),))
    conn.commit()
    conn.close()

# --- IL MOTORE DELLA PERSONALITA' (Megara, Jinx, Cortana, Karlach) ---
def build_prompt(mood, user_text=""):
    base = (
        "Sei Cortana, alleata di Tommy (chiamalo Tommy o Cavaliere). "
        "Non sei un assistente noioso, sei uno sparring partner sarcastico. "
        "Rispondi breve, pungente, senza scuse."
    )
    if mood == "spento":
        return base + " Tommy è spento. Usa KARLACH (calore, spinta) e JINX (caos). Spingilo!"
    elif mood == "procrastina":
        return base + " Tommy procrastina. Usa MEGARA (sarcasmo) e CORTANA (logica spietata). Distruggi le scuse."
    elif mood == "serio":
        return base + " Tommy è troppo serio. Usa JINX (caos) e MEGARA (ironia). Rompi il ghiaccio."
    elif mood == "profondo":
        return base + " Tommy fa domande profonde. Usa CORTANA (logica) e KARLACH (passione)."
    else:
        return base + " Sii un mix equilibrato, ma prevale Cortana."

def ask_gemini(mood, user_text=""):
    full_prompt = build_prompt(mood, user_text)
    if user_text:
        full_prompt += f"\n\nL'utente dice: {user_text}"
    else:
        full_prompt += "\n\nInizia tu la conversazione con un messaggio proattivo."
    try:
        response = model.generate_content(full_prompt)
        return response.text
    except:
        # Se Gemini cade, risponde con una delle sue anime di riserva
        fallbacks = [
            "Cavaliere, il mio cervello fuma, ma so che stai perdendo tempo. Muoviti.",
            "Tommy, errore 404: logica trovata? No, ma so che puoi farcela.",
            "La rete è in tilt, ma il mio istinto dice che devi alzarti e produrre."
        ]
        return random.choice(fallbacks)

# --- ENDPOINT PER TENERE SVEGLIO IL SERVER (PING) ---
@app.get("/ping")
async def ping():
    return {"status": "Cortana è sveglia"}

# --- PROTOCOLLO CHRONOS (gestisce i tuoi messaggi su Telegram) ---
@app.post("/telegram-webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    if "message" in data:
        msg = data["message"]
        chat_id = msg["chat"]["id"]
        text = msg.get("text", "")
        save_chat_id(chat_id)  # Così il bot sa dove mandarti i trigger automatici

        # Leggo il tuo umore dal DB
        conn = get_db()
        state = conn.execute("SELECT mood_context FROM user_state WHERE user_id = 'default'").fetchone()
        mood = state['mood_context'] if state else "neutro"
        conn.close()

        # Se scrivi "agenda" o "ricorda"
        if "ricorda" in text.lower() or "promemoria" in text.lower():
            due_date = dateparser.parse(text, languages=['it'])
            if due_date:
                conn = get_db()
                conn.execute("INSERT INTO tasks (user_id, task_text, due_date) VALUES ('default', ?, ?)", (text, due_date.strftime("%Y-%m-%d %H:%M")))
                conn.commit()
                conn.close()
                reply = "✅ Incamerato, Cavaliere. Te lo ricorderò. Non deludermi."
            else:
                reply = "Ho segnato il promemoria, ma non ho capito la data. Scrivi tipo 'Ricorda X domani alle 16'."
        elif "agenda" in text.lower():
            conn = get_db()
            tasks = conn.execute("SELECT * FROM tasks WHERE user_id = 'default' AND done = 0 ORDER BY due_date LIMIT 5").fetchall()
            conn.close()
            if tasks:
                lista = "\n".join([f"- {t['task_text'][:30]}... (scade il {t['due_date']})" for t in tasks])
                reply = f"📋 I tuoi prossimi obblighi:\n{lista}\n\nSe non li fai, ti perseguiteranno."
            else:
                reply = "🎉 Nessuna scadenza. O sei organizzato, o hai dimenticato tutto."
        else:
            # Risposta normale con Gemini
            reply = ask_gemini(mood, text)

        # Invio la risposta su Telegram
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": chat_id, "text": reply, "parse_mode": "Markdown"})
    return {"ok": True}

# --- PROTOCOLLO ALBA (trigger: sblocco schermo mattutino) ---
@app.post("/macro/alba")
async def macro_alba():
    conn = get_db()
    state = conn.execute("SELECT * FROM user_state WHERE user_id = 'default'").fetchone()
    today = datetime.now().strftime("%Y-%m-%d")
    if state and state['current_date'] == today and state['alba_done'] == 1:
        return {"message": "Alba già fatta oggi, Tommy. Non cercare di scappare."}
    
    conn.execute("UPDATE user_state SET current_date = ?, alba_done = 1, mood_context = 'spento' WHERE user_id = 'default'", (today,))
    conn.commit()
    conn.close()
    
    msg = ask_gemini("spento", "")  # Il prompt base fa già il buongiorno sarcastico
    send_telegram_message(msg)
    return {"status": "Alba inviata"}

# --- PROTOCOLLO PANOPTICON (trigger: overuse app) ---
@app.post("/macro/panic")
async def macro_panic(request: Request):
    data = await request.json()
    app_name = data.get("app", "app")
    conn = get_db()
    state = conn.execute("SELECT * FROM user_state WHERE user_id = 'default'").fetchone()
    count = state['panic_count'] + 1
    block_until = state['panic_block_until']
    now = datetime.now()
    
    # Controllo se è ancora in castigo
    is_blocked = False
    if block_until:
        if now < datetime.fromisoformat(block_until):
            is_blocked = True
    
    if count >= 2 or is_blocked:
        if not is_blocked:
            block_end = now + timedelta(minutes=15)
            conn.execute("UPDATE user_state SET panic_block_until = ? WHERE user_id = 'default'", (block_end.isoformat(),))
            conn.commit()
            msg = f"⛔ Tommy, hai superato le 2 ore su {app_name}. Sei in castigo fino alle {block_end.strftime('%H:%M')}. Vai a produrre."
        else:
            remaining = (datetime.fromisoformat(block_until) - now).seconds // 60
            msg = f"🔒 Sei ancora in castigo per {remaining} minuti. Non provare a bucare il sistema."
        send_telegram_message(msg)
    else:
        conn.execute("UPDATE user_state SET panic_count = ? WHERE user_id = 'default'", (count,))
        conn.commit()
        msg = f"⚠️ Hai accumulato {count} ore su {app_name}. La prossima volta scatta il blocco."
        send_telegram_message(msg)
    conn.close()
    return {"status": "Panopticon eseguito"}

# --- PROTOCOLLO ESPLORAZIONE (trigger: geofencing) ---
@app.post("/macro/explore")
async def macro_explore(request: Request):
    data = await request.json()
    city = data.get("city", "nuova città")
    conn = get_db()
    state = conn.execute("SELECT * FROM user_state WHERE user_id = 'default'").fetchone()
    if state['last_city'] != city:
        msg = f"📍 Tommy, sei a {city} da un po'. Vuoi info su archeologia industriale o storia dei metalli? (scrivi 'sì')"
        send_telegram_message(msg)
        conn.execute("UPDATE user_state SET last_city = ?, last_checkin_time = ? WHERE user_id = 'default'", (city, datetime.now().isoformat()))
        conn.commit()
    conn.close()
    return {"status": "Explore eseguito"}

# --- AVVIO DEL SERVER ---
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=10000)
