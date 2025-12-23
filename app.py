import streamlit as st
import os
import json
import gspread
from datetime import datetime
from groq import Groq
from dotenv import load_dotenv
from audiorecorder import audiorecorder
from google.oauth2.service_account import Credentials

# 1. Setup
load_dotenv()

# CSS Hack für große Buttons (Handschuh-Modus) & bessere Lesbarkeit
st.markdown("""
    <style>
    .stAudioRecorder { transform: scale(2.0); margin: 40px auto; display: flex; justify_content: center; }
    button { height: 3em !important; font-size: 20px !important; }
    div[data-baseweb="select"] { transform: scale(1.05); }
    .stAlert { font-weight: bold; }
    </style>
    """, unsafe_allow_html=True)

if not os.getenv("GROQ_API_KEY"):
    st.error("❌ API Key fehlt! Bitte in den Secrets prüfen.")
    st.stop()

client = Groq(api_key=os.getenv("GROQ_API_KEY"))

# --- 2. KONFIGURATION: GEWERKE & FACHBEGRIFFE ---
GEWERKE_KONTEXT = {
    "Sanitär & Heizung": {
        "whisper_keywords": "Rohre, Muffen, Fittinge, Hanf, Abwasser, HT-Rohr, Kupfer, Siphon, Kessel, Heizkörper, Ventil, Presszange",
        "llama_role": "Du bist ein Bauleiter für Sanitär und Heizung (SHK)."
    },
    "Elektro": {
        "whisper_keywords": "Kabel, Ader, Litze, Schalter, Steckdose, FI-Schalter, Sicherung, Klemme, Wago, Schlitz, Dose, Spannung, Volt, Ampere",
        "llama_role": "Du bist ein Bauleiter für Elektrotechnik."
    },
    "Trockenbau": {
        "whisper_keywords": "Gipskarton, Ständerwerk, Profile, UW-Profil, CW-Profil, Spachteln, Schleifen, Dämmung, Dampfbremse, Rigips, Schrauben",
        "llama_role": "Du bist ein Bauleiter für Trockenbau."
    },
    "Maler & Lackierer": {
        "whisper_keywords": "Farbe, Lack, Grundierung, Abkleben, Spachtel, Vlies, Tapezieren, Dispersionsfarbe, Rolle, Pinsel, Q3, Q4",
        "llama_role": "Du bist ein Bauleiter für Malerarbeiten."
    },
    "Allgemein / Bauleitung": {
        "whisper_keywords": "Baustelle, Begehung, Abnahme, Mangel, Behinderung, Rapport, Stunden, Besprechung",
        "llama_role": "Du bist ein allgemeiner Bauleiter."
    }
}

# --- 3. LOGIK FUNKTIONEN ---

def save_to_google_sheets(daten, gewerk):
    try:
        # Erweiteter Scope für Schreibzugriff
        scope = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive"
        ]
        
        creds_dict = st.secrets["gcp_service_account"]
        creds = Credentials.from_service_account_info(creds_dict, scopes=scope)
        gc = gspread.authorize(creds)
        
        # Den Namen der Tabelle exakt so wie im Screenshot (Groß/Kleinschreibung!)
        sheet = gc.open("Logbook").worksheet("Berichte")
                
        mat_liste = daten.get('material_verbraucht', [])
        mat_string = " | ".join([f"{m['menge']} {m['einheit']} {m['artikel']}" for m in mat_liste])
        
        zeile = [
            datetime.now().strftime("%d.%m.%Y %H:%M"),
            gewerk,
            daten.get("taetigkeit", "-"),
            daten.get("arbeitszeit", 0.0),
            mat_string,
            "OK"
        ]
        sheet.append_row(zeile)
        return True
    except Exception as e:
        st.error(f"Fehler beim Speichern in Google Sheets: {e}")
        return False

def process_audio(audio_bytes, gewerk_name):
    keywords = GEWERKE_KONTEXT[gewerk_name]["whisper_keywords"]
    with open("temp_audio.wav", "wb") as f:
        f.write(audio_bytes)
    with open("temp_audio.wav", "rb") as file:
        transcript = client.audio.transcriptions.create(
            model="whisper-large-v3", 
            file=file,
            prompt=f"Bericht Bereich {gewerk_name}. Fachbegriffe: {keywords}."
        )
    return transcript.text

def analyze_text(text, gewerk_name):
    role_description = GEWERKE_KONTEXT[gewerk_name]["llama_role"]
    system_prompt = f"""
    {role_description}
    TASK: Extract data into JSON. 
    
    UNIVERSAL LANGUAGE RULES:
    1. Always translate 'taetigkeit' and 'artikel' into GERMAN.
    2. Always write 'fehlende_infos' in the SAME LANGUAGE the user used (English, Polish, etc.).
    
    STRICT VALIDATION:
    - Status 'RUECKFRAGE_NOETIG' if items like 'pipes' lack diameter/material.
    - Status 'RUECKFRAGE_NOETIG' if quantities are vague.
    
    JSON STRUCTURE:
    {{
        "logbuch_eintrag": {{ 
            "taetigkeit": "string", 
            "arbeitszeit": float, 
            "material_verbraucht": [{{ "artikel": str, "menge": float, "einheit": str }}] 
        }},
        "status": "OK" | "RUECKFRAGE_NOETIG",
        "fehlende_infos": "string in user's language"
    }}
    """
    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile", 
        response_format={ "type": "json_object" }, 
        messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": text}],
        temperature=0
    )
    return json.loads(response.choices[0].message.content)

def update_entry(altes_json, neue_info):
    system_prompt_update = """
    ROLE: Precise Construction Data Merger.
    TASK: Integrate NEW_INPUT into OLD_JSON without losing quantities or dimensions.
    
    STRICT RULES:
    1. QUANTITY PRESERVATION: If OLD_JSON or NEW_INPUT mentions a number (e.g., '6 pieces', '3 meters'), it MUST appear in the 'menge' or 'artikel' field.
    2. DETAIL MERGING: If the user adds a dimension (e.g., '16 Zoll'), append it to the 'artikel' name (e.g., 'Kupferrohr 16 Zoll').
    3. NO DATA LOSS: Do not overwrite 6 pieces with 1 piece unless explicitly corrected.
    4. TRANSLATION: Keep everything in GERMAN for the final JSON values.
    """
    
    user_message = f"OLD_JSON: {json.dumps(altes_json)}\nNEW_INPUT: {neue_info}"
    
    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile", 
        response_format={ "type": "json_object" }, 
        messages=[{"role": "system", "content": system_prompt_update}, {"role": "user", "content": user_message}],
        temperature=0
    )
    return json.loads(response.choices[0].message.content)

# --- 4. APP OBERFLÄCHE ---

st.title("🏗️ Logbook | Smart Bau-Tagebuch")

selected_gewerk = st.selectbox("🔧 Wähle dein Gewerk:", list(GEWERKE_KONTEXT.keys()))

if 'step' not in st.session_state:
    st.session_state.step = 1
if 'current_data' not in st.session_state:
    st.session_state.current_data = None

audio = audiorecorder("🎙️ Aufnahme starten", "⏹️ Stop")

# Audio Verarbeitung
if len(audio) > 0 and st.session_state.step == 1:
    with st.spinner(f"Verarbeite für {selected_gewerk}..."):
        text = process_audio(audio.export().read(), selected_gewerk)
        st.info(f"📝 Erkannt: {text}")
        st.session_state.current_data = analyze_text(text, selected_gewerk)
        st.session_state.step = 2

# Ergebnis Anzeige
if st.session_state.step >= 2 and st.session_state.current_data:
    data = st.session_state.current_data
    log = data.get("logbuch_eintrag", {})
    
    if log:
        st.subheader("✅ Vorschau Tagebuch")
        with st.container(border=True):
            st.write(f"**Tätigkeit:** {log.get('taetigkeit', '-')}")
            st.metric("Zeit", f"{log.get('arbeitszeit', 0)} Std")
            if log.get("material_verbraucht"):
                st.write("**Material:**")
                for mat in log.get("material_verbraucht"):
                    # Nutze .get() mit Fallback, damit es nie wieder crasht
                    m = mat.get('menge', '?')
                    e = mat.get('einheit', '')
                    a = mat.get('artikel', 'Unbekanntes Material')
                    st.text(f"• {m} {e} {a}")

    bestellung = data.get("material_bestellung", {})
    if bestellung.get("hat_bestellung"):
        st.divider()
        st.subheader("📦 Bestellung erkannt")
        with st.warning("Materialliste", icon="🚛"):
            st.dataframe(bestellung.get("items"), hide_index=True)

    if data.get("status") == "RUECKFRAGE_NOETIG":
        st.divider()
        st.warning(f"🤔 **KI-Rückfrage:** {data.get('fehlende_infos')}")
        audio_antwort = audiorecorder("🎙️ Antwort einsprechen", "⏹️ Absenden", key="answer_rec")
        if len(audio_antwort) > 0:
            with st.spinner("Ergänze Daten..."):
                antwort_text = process_audio(audio_antwort.export().read(), selected_gewerk)
                st.session_state.current_data = update_entry(data, antwort_text)
                st.rerun()

    st.divider()
    col_save, col_reset = st.columns(2)
    
    with col_save:
        if st.button("💾 In Google Sheets speichern", type="primary"):
            if save_to_google_sheets(log, selected_gewerk):
                st.toast("In Google Sheets gespeichert!", icon="✅")
                st.session_state.step = 1
                st.session_state.current_data = None
                st.rerun()

    with col_reset:
        if st.button("🔄 Verwerfen"):
            st.session_state.step = 1
            st.session_state.current_data = None
            st.rerun()