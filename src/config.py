"""
MeshBridge — Configuration centralisée.
Charge le .env (racine du repo) et expose toutes les constantes.
"""
import os
from pathlib import Path
from dotenv import load_dotenv

# .env cherché à la racine du repo quel que soit le répertoire courant
# (indispensable si le bridge est lancé par systemd ou depuis ailleurs)
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# --- Radio / LoRa ---
MAX_LEN            = 200          # limite d'un message LoRa (octets)
MESHBRIDGE_CHANNEL = 1            # canal privé écouté ; on ignore LongFast (0)

# --- Réseau ---
HTTP_TIMEOUT = 10                 # secondes

# --- /web : garde-fous ---
# Le Pi télécharge des pages arbitraires : on plafonne la taille (mémoire),
# on limite les redirections (chaque saut est re-contrôlé anti-SSRF).
WEB_MAX_BYTES     = 500_000       # 500 Ko max téléchargés par page
WEB_MAX_REDIRECTS = 3

# --- File de commandes ---
# Bornée : un flood de commandes ne doit ni remplir la RAM ni générer
# une rafale de réponses (budget duty cycle 10 %). Au-delà, on jette
# silencieusement (loggé) plutôt que d'amplifier le trafic radio.
QUEUE_MAX = 5

# --- IA locale (Ollama) ---
OLLAMA_URL       = "http://localhost:11434/api/generate"
OLLAMA_MODEL     = "llama3.2:1b"
OLLAMA_TIMEOUT_S = 60             # un petit modèle sur Pi peut être lent

# --- IA cloud (Gemini) ---
GEMINI_KEY       = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL     = "gemini-3.5-flash"
GEMINI_TIMEOUT_S = 30             # sans timeout, une API qui pend gèle le worker

# --- Interface radio : BLE uniquement ---
# Pierre est physiquement séparé du Pi (contre la fenêtre, mieux placé
# côté LoRa). Le lien Pi ↔ Pierre se fait par Bluetooth Low Energy.
PIERRE_BLE_MAC = os.environ.get("PIERRE_BLE_MAC", "")

# Reconnexion automatique : le BLE peut décrocher (interférences WiFi
# 2.4 GHz, distance, Pierre qui reboot). Le bridge relance l'interface
# après chaque déconnexion, avec un délai qui augmente pour ne pas
# saturer le bus Bluetooth en cas d'échec répété.
BLE_RECONNECT_MIN_S = 5      # premier délai de retry
BLE_RECONNECT_MAX_S = 60     # plafond du délai de retry

# --- Autorisation : dialogue chiffré avec Paul ---
# Le bridge n'obéit qu'aux messages directs chiffrés (PKC Meshtastic) venant
# de Paul. PAUL_NODE_ID est l'identifiant du nœud, lu dans l'app Meshtastic
# (détails de Paul). Le chiffrement de bout en bout est assuré par le firmware.


def _node_id_to_num(raw: str) -> int | None:
    """!a1b2c3d4 (ou décimal) → numéro de nœud. None si vide ou invalide."""
    raw = raw.strip()
    if not raw:
        return None
    try:
        return int(raw[1:], 16) if raw.startswith("!") else int(raw)
    except ValueError:
        return None


PAUL_NODE_NUM = _node_id_to_num(os.environ.get("PAUL_NODE_ID", ""))

# --- Choix de l'IA ---
# "auto"  : Gemini si dispo, sinon bascule sur Ollama (recommandé)
# "cloud" : force Gemini (échoue en local → fallback brut)
# "local" : force Ollama (vrai off-grid garanti)
# Surchargeable par requête avec un suffixe : "/ask ... !local" ou "!cloud"
AI_MODE = "auto"

# --- Comportement ---
# Accusé de réception immédiat ("⏳") pour les commandes lentes : sans
# feedback pendant 30-60 s, l'utilisateur tend à renvoyer sa commande —
# ce qui coûte plus d'airtime que l'ACK (~20 octets, négligeable à un
# rythme humain). Passer à False pour économiser chaque émission.
SEND_ACK = True
ACK_TEXT = "⏳ traitement…"