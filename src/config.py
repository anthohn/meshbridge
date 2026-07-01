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

# --- Interface radio : BLE uniquement ---
# Aurora est physiquement séparé du Pi (contre la fenêtre, mieux placé
# côté LoRa). Le lien Pi ↔ Aurora se fait par Bluetooth Low Energy.
AURORA_BLE_MAC = os.environ.get("AURORA_BLE_MAC", "")

# Reconnexion automatique : le BLE peut décrocher (interférences WiFi
# 2.4 GHz, distance, Aurora qui reboot). Le bridge relance l'interface
# après chaque déconnexion, avec un délai qui augmente pour ne pas
# saturer le bus Bluetooth en cas d'échec répété.
BLE_RECONNECT_MIN_S = 5      # premier délai de retry
BLE_RECONNECT_MAX_S = 60     # plafond du délai de retry

# --- Choix de l'IA ---
# "auto"  : Gemini si dispo, sinon bascule sur Ollama (recommandé)
# "cloud" : force Gemini (échoue en local → fallback brut)
# "local" : force Ollama (vrai off-grid garanti)
# Surchargeable par requête avec un suffixe : "/ask ... !local" ou "!cloud"
AI_MODE = "auto"

# --- Comportement ---
# Accusé de réception immédiat ("⏳") pour les commandes lentes.
# Désactivé par défaut : chaque accusé = une émission radio en plus
# (consomme le budget légal des 10 % d'airtime/heure). Passe à True
# si tu préfères le confort du feedback immédiat.
SEND_ACK = False
ACK_TEXT = "⏳ traitement…"