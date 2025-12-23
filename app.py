import streamlit as st
import os
import json
import csv
from datetime import datetime
from groq import Groq
from dotenv import load_dotenv
from audiorecorder import audiorecorder

# 1. Setup
load_dotenv()

# CSS Hack für große Buttons (Handschuh-Modus) & bessere Lesbarkeit
st.markdown("""
    <style>
    .stAudioRecorder { transform: scale(2.0); margin: 40px auto; display: flex; justify_content: center; }
    button { height: 3em !important; font-size: 20px !important; }
    div[data-baseweb="select"] { transform: scale(1.05); }
    /* Warn-Boxen etwas auffälliger machen */
    .stAlert { font-weight: bold; }
    </style>
    """, unsafe_allow_html=True)

if not os.getenv("GROQ_API_KEY"):
    st.error("❌ API Key fehlt! Bitte .env Datei prüfen.")
    st.stop()

client = Groq(api_key=os.getenv("GROQ_API_KEY"))
CSV_DATEI = "baustellentagebuch.csv"

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

def process_audio(audio_bytes, gewerk_name):
    keywords = GEWERKE_KONTEXT[gewerk_name]["whisper_keywords"]
    with open("temp_audio.wav", "wb") as f:
        f.write(audio_bytes)
    with open("temp_audio.wav", "rb") as file:
        transcript = client.audio.transcriptions.create(
            model="whisper-large-v3", 
            file=file,
            prompt=f"Hier ist ein Bericht aus dem Bereich {gewerk_name}. Fachbegriffe: {keywords}. Ganze Sätze."
        )
    return transcript.text

def analyze_text(text, gewerk_name):
    role_description = GEWERKE_KONTEXT[gewerk_name]["llama_role"]
    system_prompt = f"""
    {role_description}
    DEINE AUFGABE (MULTI-INTENT):
    Trenne strikt zwischen:
    1. TAGEBUCH (Was wurde getan? Vergangenheit)
    2. BESTELLUNG (Was wird gebraucht? Zukunft)
    
    JSON STRUKTUR:
    {{
        "logbuch_eintrag": {{
            "taetigkeit": "string (Fachsprache Deutsch)",
            "arbeitszeit": float,
            "material_verbraucht": [ {{ "artikel": "string", "menge": float, "einheit": "string" }} ]
        }},
        "material_bestellung": {{
            "hat_bestellung": boolean,
            "deadline": "string oder null",
            "items": [ {{ "artikel": "string", "menge": float, "einheit": "string" }} ]
        }},
        "status": "OK" | "RUECKFRAGE_NOETIG" | "IGNORED",
        "fehlende_infos": "string"
    }}
    WICHTIG: "Brauche 5 Platten" -> kommt in Bestellung, NICHT in Verbrauch!
    """
    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile", 
        response_format={ "type": "json_object" }, 
        messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": text}],
        temperature=0
    )
    return json.loads(response.choices[0].message.content)

def update_entry(altes_json, neue_info):
    # Einfache Update-Logik (Könnte man bei Bedarf noch verfeinern)
    system_prompt_update = "Du bist ein Datenbank-Updater. Integriere die neue Info in das JSON. Setze Status auf OK wenn komplett."
    user_message = f"ALTES JSON: {json.dumps(altes_json)}\nNEUE INFO: {neue_info}"
    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile", 
        response_format={ "type": "json_object" }, 
        messages=[{"role": "system", "content": system_prompt_update}, {"role": "user", "content": user_message}],
        temperature=0
    )
    return json.loads(response.choices[0].message.content)

def save_to_csv(daten, gewerk):
    # WICHTIG: 'daten' ist hier nur der 'logbuch_eintrag' Teil!
    datei_existiert = os.path.exists(CSV_DATEI)
    with open(CSV_DATEI, mode='a', newline='', encoding='utf-8') as file:
        writer = csv.writer(file, delimiter=';')
        if not datei_existiert:
            writer.writerow(["Datum", "Gewerk", "Tätigkeit", "Stunden", "Material_Verbrauch", "Status"])
        
        # Hier greifen wir auf 'material_verbraucht' zu (neues Schema)
        mat_liste = daten.get('material_verbraucht', [])
        # Fallback falls es mal 'material_liste' heißt (Sicherheit)
        if not mat_liste: 
            mat_liste = daten.get('material_liste', [])

        mat_string = " | ".join([f"{m['menge']} {m['einheit']} {m['artikel']}" for m in mat_liste])
        
        writer.writerow([
            datetime.now().strftime("%Y-%m-%d %H:%M"),
            gewerk,
            daten.get("taetigkeit", ""),
            daten.get("arbeitszeit", 0.0),
            mat_string,
            "OK"
        ])

# --- 4. APP OBERFLÄCHE ---

st.title("🏗️ Logbook | Smart Bau-Tagebuch")

selected_gewerk = st.selectbox("🔧 Wähle dein Gewerk:", list(GEWERKE_KONTEXT.keys()))
st.write("Sprich deinen Bericht ein:")

audio = audiorecorder("🎙️ Aufnahme starten", "⏹️ Stop")

if 'step' not in st.session_state:
    st.session_state.step = 1
if 'current_data' not in st.session_state:
    st.session_state.current_data = None

# Audio Verarbeitung
if len(audio) > 0 and st.session_state.step == 1:
    st.audio(audio.export().read())
    with st.spinner(f"Verarbeite für {selected_gewerk}..."):
        text = process_audio(audio.export().read(), selected_gewerk)
        st.info(f"📝 Erkannt: {text}")
        data = analyze_text(text, selected_gewerk)
        st.session_state.current_data = data
        st.session_state.step = 2

# Ergebnis Anzeige
if st.session_state.step >= 2 and st.session_state.current_data:
    data = st.session_state.current_data
    
    # --- 1. Der Tagebuch-Eintrag (Vergangenheit) ---
    log = data.get("logbuch_eintrag", {})
    
    if log:
        st.subheader("✅ Tagebuch-Eintrag")
        # Container verhindert das Abschneiden von Text
        with st.container(border=True):
            c1, c2 = st.columns([3, 1]) 
            
            with c1:
                st.markdown("**Tätigkeit:**")
                st.write(log.get("taetigkeit", "-")) # st.write bricht Text um -> kein "..." mehr!
                
                if log.get("material_verbraucht"):
                    st.markdown("**Verbrauchtes Material:**")
                    for mat in log.get("material_verbraucht"):
                        st.text(f"• {mat['menge']} {mat['einheit']} {mat['artikel']}")

            with c2:
                # Metrik für Zahlen ist okay
                st.metric("Zeit", f"{log.get('arbeitszeit', 0)} Std")

    # --- 2. Die Material-Bestellung (Zukunft) ---
    bestellung = data.get("material_bestellung", {})
    if bestellung.get("hat_bestellung"):
        st.divider()
        st.subheader("📦 Bestellung erkannt")
        with st.warning("Details zur Bestellung", icon="🚛"):
            if bestellung.get("deadline"):
                st.write(f"**Fällig bis:** {bestellung.get('deadline')}")
            st.dataframe(bestellung.get("items"), hide_index=True)

    # --- 3. Rohdaten & Rückfragen ---
    if data.get("status") == "RUECKFRAGE_NOETIG":
        st.warning(f"⚠️ Rückfrage: {data.get('fehlende_infos')}")
        antwort = st.chat_input("Antwort eingeben...")
        if antwort:
            with st.spinner("Aktualisiere..."):
                neu_data = update_entry(data, antwort)
                st.session_state.current_data = neu_data
                st.rerun()

    # --- 4. Speichern ---
    st.divider()
    with st.expander("🔍 Rohe Daten anzeigen (Debug)"):
        st.json(data)

    col_save, col_reset = st.columns([1, 1])
    
    with col_save:
        if data.get("status") == "OK":
            # Wir übergeben nur 'log' (den Tagebuch-Teil) an die CSV Funktion!
            if st.button("💾 Tagebuch speichern", type="primary"):
                save_to_csv(log, selected_gewerk) 
                st.toast("Eintrag gespeichert!", icon="💾")
                st.session_state.step = 1
                st.session_state.current_data = None
                st.rerun()

    with col_reset:
        if st.button("🔄 Verwerfen"):
            st.session_state.step = 1
            st.session_state.current_data = None
            st.rerun()