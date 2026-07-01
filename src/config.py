"""
MeshBridge — Configuration centralisée.
Charge le .env (racine du repo) et expose toutes les constantes.
"""
import os
from dotenv import load_dotenv

load_dotenv()  # lit le .env à la racine du repo

# --- Radio / LoRa ---
MAX_LEN            = 200          # limite d'un message LoRa (octets)
MESHBRIDGE_CHANNEL = 1            # canal privé écouté ; on ignore LongFast (0)

# --- Réseau ---
HTTP_TIMEOUT = 10                 # secondes

# --- IA locale (Ollama) ---
OLLAMA_URL   = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "llama3.2:1b"

# --- IA cloud (Gemini) ---
GEMINI_KEY   = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = "gemini-3.5-flash"

# --- Comportement ---
SEND_ACK = True                   # accusé de réception immédiat pour les
ACK_TEXT = "⏳ traitement…"        # commandes lentes (web/ask/news)
