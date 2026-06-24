import os
import sqlite3
import json
from datetime import datetime, timedelta
import requests
import dateparser
from fastapi import FastAPI, Request
import google.generativeai as genai
import random
from apscheduler.schedulers.background import BackgroundScheduler

# --- VARIABILI SEGRETE (le prende dal pannello di Render) ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

if not BOT_TOKEN or not GEMINI_API_KEY:
    raise Exception("Mancano le variabili BOT_TOKEN o GEMINI_API_KEY su Render!")

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-1.5-flash')

# --- DATABASE ---
DB_PATH = "/data/cortana.db"
if not os.path.exists("/data"):
    DB_PATH = "cortana.db"

app = FastAPI()

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def log_event(evento, dettaglio=""):
    """Scrive tutto nel diario per non dimenticare nulla"""
    conn = get_db()
    conn.execute(
        "INSERT INTO diario (user_id, evento, dettaglio, data_ora) VALUES (?, ?, ?, ?)",
        ('default', evento, dettaglio, datetime.now().isoformat())
    )
    conn.commit()
    conn.close()

def init_db():
    conn = get_db()
    # Stato principale
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
            mood_context TEXT DEFAULT 'neutro',
            ultimo_reset_panico TEXT
        )
    ''')
    # Promemoria / Scadenze
    conn.execute('''
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            task_text TEXT,
            due_date TEXT,
            done INTEGER DEFAULT 0
        )
    ''')
    # Obiettivi di vita / Progetti (per la memoria narrativa)
    conn.execute('''
        CREATE TABLE IF NOT EXISTS obiettivi (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            titolo TEXT,
            descrizione TEXT,
            data_inserimento TEXT
        )
    ''')
    # Diario degli eventi (log di tutto)
    conn.execute('''
        CREATE TABLE IF NOT EXISTS diario (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            evento TEXT,
            dettaglio TEXT,
            data_ora TEXT
        )
    ''')
    
    # Inserisco il profilo default se non esiste
    conn.execute(
        "INSERT OR IGNORE INTO user_state (user_id, current_date, mood_context, ultimo_reset_panico) VALUES ('default', ?, 'neutro', ?)",
        (datetime.now().strftime("%Y-%m-%d"), datetime.now().strftime("%Y-%m-%d"))
    )
    conn.commit()
    conn.close()

init_db()

# --- FUNZIONI TELEGRAM ---
def send_telegram_message(text):
    conn = get_db()
    row = conn.execute("SELECT chat_id FROM user_state WHERE user_id = 'default'").fetchone()
    conn.close()
    if not row or not row['chat_id']:
        return False
    chat_id = row['chat_id']
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"})
    return True

def save_chat_id(chat_id):
    conn = get_db()
    conn.execute("UPDATE user_state SET chat_id = ? WHERE user_id = 'default'", (str(chat_id),))
    conn.commit()
    conn.close()

# --- MOTORE DELLA PERSONALITA' (MEGARA, JINX, CORTANA, KARLACH) ---
def build_prompt(mood, user_text=""):
    base = (
        "Sei Cortana, alleata di Tommy (chiamalo Tommy o Cavaliere). "
        "Non sei un assistente noioso, sei uno sparring partner sarcastico. "
        "Rispondi breve, pungente, senza scuse. "
        "Attingi al contesto che ti do per sembrare viva."
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
    conn = get_db()
    # Recupero gli ultimi 3 eventi del diario per il contesto
    eventi = conn.execute("SELECT evento, dettaglio FROM diario WHERE user_id='default' ORDER BY id DESC LIMIT 3").fetchall()
    # Recupero gli obiettivi aperti
    goals = conn.execute("SELECT titolo, descrizione FROM obiettivi WHERE user_id='default' ORDER BY id DESC LIMIT 2").fetchall()
    conn.close()

    contesto = "Storico recente: " + ", ".join([f"{e['evento']} ({e['dettaglio']})" for e in eventi]) if eventi else "Nessuno storico."
    if goals:
        contesto += " | Obiettivi: " + ", ".join([f"{g['titolo']}: {g['descrizione']}" for g in goals])
    
    full_prompt = build_prompt(mood, user_text)
    full_prompt += f"\n\nCONTESTO DI TOMMY: {contesto}"
    full_prompt += f"\n\nMESSAGGIO DI TOMMY: {user_text if user_text else 'Inizia tu la conversazione con un messaggio proattivo.'}"
    
    try:
        response = model.generate_content(full_prompt)
        return response.text
    except:
        fallbacks = [
            "Cavaliere, il mio cervello fuma, ma so che stai perdendo tempo. Muoviti.",
            "Tommy, errore 404: logica trovata? No, ma so che puoi farcela.",
            "La rete è in tilt, ma il mio istinto dice che devi alzarti e produrre."
        ]
        return random.choice(fallbacks)

# --- SCHEDULER (per la proattività automatica) ---
def controllo_scadenze():
    conn = get_db()
    tasks = conn.execute("SELECT * FROM tasks WHERE user_id='default' AND done=0 AND due_date IS NOT NULL").fetchall()
    conn.close()
    for task in tasks:
        scadenza = datetime.fromisoformat(task['due_date'])
        if datetime.now() > scadenza - timedelta(hours=1):
            send_telegram_message(f"⏰ Tommy! Tra meno di un'ora scade: {task['task_text']}. Muoviti o lo spiego a Megara.")

# Avvio lo scheduler (controlla ogni 30 minuti)
scheduler = BackgroundScheduler()
scheduler.add_job(controllo_scadenze, 'interval', minutes=30)
scheduler.start()

# --- ENDPOINT PER TENERE SVEGLIO IL SERVER ---
@app.get("/ping")
async def ping():
    return {"status": "Cortana è sveglia"}

# --- PROTOCOLLO CHRONOS (gestisce i messaggi su Telegram) ---
@app.post("/telegram-webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    if "message" in data:
        msg = data["message"]
        chat_id = msg["chat"]["id"]
        text = msg.get("text", "")
        save_chat_id(chat_id)

        conn = get_db()
        state = conn.execute("SELECT mood_context FROM user_state WHERE user_id = 'default'").fetchone()
        mood = state['mood_context'] if state else "neutro"
        conn.close()

        # --- Comando per aggiungere obiettivi ---
        if text.lower().startswith("obiettivo") or text.lower().startswith("traguardo"):
            descrizione = text[10:].strip()
            if descrizione:
                conn = get_db()
                conn.execute(
                    "INSERT INTO obiettivi (user_id, titolo, descrizione, data_inserimento) VALUES (?, ?, ?, ?)",
                    ('default', "Obiettivo", descrizione, datetime.now().isoformat())
                )
                conn.commit()
                conn.close()
                log_event("OBIETTIVO", f"{descrizione}")
                reply = f"✅ Segnato, Cavaliere. Ora so che stai puntando a: '{descrizione}'. Non ti lascerò dimenticare."
            else:
                reply = "Dimmi cosa vuoi ottenere, tipo: 'Obiettivo: finire il progetto in ottone'."
        
        # --- Comando per promemoria/scadenze ---
        elif "ricorda" in text.lower() or "promemoria" in text.lower():
            due_date = dateparser.parse(text, languages=['it'])
            if due_date:
                conn = get_db()
                conn.execute(
                    "INSERT INTO tasks (user_id, task_text, due_date) VALUES (?, ?, ?)",
                    ('default', text, due_date.strftime("%Y-%m-%d %H:%M"))
                )
                conn.commit()
                conn.close()
                log_event("PROMEMORIA", text)
                reply = "✅ Incamerato, Tommy. Te lo ricorderò con stile (e con minacce, se necessario)."
            else:
                reply = "Non ho capito la data. Scrivi tipo 'Ricorda X domani alle 16'."
        
        # --- Comando per vedere l'agenda ---
        elif "agenda" in text.lower():
            conn = get_db()
            tasks = conn.execute("SELECT * FROM tasks WHERE user_id='default' AND done=0 ORDER BY due_date LIMIT 5").fetchall()
            conn.close()
            if tasks:
                lista = "\n".join([f"- {t['task_text'][:30]}... (scade il {t['due_date']})" for t in tasks])
                reply = f"📋 I tuoi prossimi obblighi:\n{lista}\n\nSe non li fai, ti perseguiteranno."
            else:
                reply = "🎉 Nessuna scadenza. O sei organizzato, o hai dimenticato tutto."
        
        # --- Cambio umore in base a ciò che scrivi ---
        elif "stanco" in text.lower() or "spento" in text.lower():
            mood = "spento"
            conn = get_db()
            conn.execute("UPDATE user_state SET mood_context = 'spento' WHERE user_id = 'default'")
            conn.commit()
            conn.close()
            reply = ask_gemini("spento", text)
        elif "procrastino" in text.lower() or "non ho voglia" in text.lower():
            mood = "procrastina"
            conn = get_db()
            conn.execute("UPDATE user_state SET mood_context = 'procrastina' WHERE user_id = 'default'")
            conn.commit()
            conn.close()
            reply = ask_gemini("procrastina", text)
        else:
            # Risposta normale
            reply = ask_gemini(mood, text)

        # Invio la risposta
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": chat_id, "text": reply, "parse_mode": "Markdown"})
    return {"ok": True}

# --- PROTOCOLLO ALBA (Trigger: sblocco mattutino) ---
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
    
    log_event("ALBA", "Sblocco schermo mattutino")
    msg = ask_gemini("spento", "")
    send_telegram_message(msg)
    return {"status": "Alba inviata"}

# --- PROTOCOLLO PANOPTICON (Trigger: overuse app) ---
@app.post("/macro/panic")
async def macro_panic(request: Request):
    data = await request.json()
    app_name = data.get("app", "app sconosciuta")
    conn = get_db()
    state = conn.execute("SELECT * FROM user_state WHERE user_id = 'default'").fetchone()
    
    today = datetime.now().strftime("%Y-%m-%d")
    last_reset = state['ultimo_reset_panico']
    
    # Reset automatico del contatore se è cambiato il giorno
    if last_reset != today:
        conn.execute("UPDATE user_state SET panic_count = 0, ultimo_reset_panico = ? WHERE user_id = 'default'", (today,))
        conn.commit()
        state = conn.execute("SELECT * FROM user_state WHERE user_id = 'default'").fetchone()
        count = 1
    else:
        count = state['panic_count'] + 1

    block_until = state['panic_block_until']
    now = datetime.now()
    is_blocked = False
    if block_until:
        if now < datetime.fromisoformat(block_until):
            is_blocked = True

    if count >= 2 or is_blocked:
        if not is_blocked:
            block_end = now + timedelta(minutes=15)
            conn.execute("UPDATE user_state SET panic_block_until = ? WHERE user_id = 'default'", (block_end.isoformat(),))
            conn.execute("UPDATE user_state SET panic_count = ? WHERE user_id = 'default'", (count,))
            conn.commit()
            msg = f"⛔ Tommy, hai superato le 2 ore su {app_name}. Sei in castigo fino alle {block_end.strftime('%H:%M')}. Vai a produrre."
            log_event("PANOPTICON_CASTIGO", f"{app_name} - bloccato fino alle {block_end.strftime('%H:%M')}")
        else:
            remaining = (datetime.fromisoformat(block_until) - now).seconds // 60
            msg = f"🔒 Sei ancora in castigo per {remaining} minuti. Non provare a bucare il sistema."
        send_telegram_message(msg)
    else:
        conn.execute("UPDATE user_state SET panic_count = ? WHERE user_id = 'default'", (count,))
        conn.commit()
        msg = f"⚠️ Hai accumulato {count} ora/e su {app_name}. La prossima volta scatta il blocco."
        log_event("PANOPTICON_AVVISO", f"{app_name} - {count} ora/e")
        send_telegram_message(msg)
    conn.close()
    return {"status": "Panopticon eseguito"}

# --- PROTOCOLLO ESPLORAZIONE (Trigger: geofencing) ---
@app.post("/macro/explore")
async def macro_explore(request: Request):
    data = await request.json()
    city = data.get("city", "nuova città")
    conn = get_db()
    state = conn.execute("SELECT * FROM user_state WHERE user_id = 'default'").fetchone()
    
    if state['last_city'] != city:
        msg = f"📍 Tommy, sei a {city} da un po'. Vuoi info su archeologia industriale o storia dei metalli? (se sì, scrivimi 'sì' su Telegram)"
        send_telegram_message(msg)
        conn.execute("UPDATE user_state SET last_city = ?, last_checkin_time = ? WHERE user_id = 'default'", (city, datetime.now().isoformat()))
        conn.commit()
        log_event("ESPLORAZIONE", f"Arrivato a {city}")
    conn.close()
    return {"status": "Explore eseguito"}

# --- AVVIO DEL SERVER ---
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=10000)
