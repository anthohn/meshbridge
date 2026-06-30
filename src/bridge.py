#!/usr/bin/env python3
"""
MeshBridge — Relai Internet off-grid via LoRa + compression IA
===============================================================
Le Raspberry Pi (à la maison, connecté à Internet) reçoit des commandes
sur le canal privé chiffré MeshBridge, va chercher l'info sur le web,
la compresse par IA (~200 octets) et la renvoie SUR LE MÊME CANAL.

Couche IA en cascade : cloud (Gemini) → local (Ollama) → troncature brute.

Commandes :
    METEO <ville>   |  NEWS  |  WEB <url>  |  ASK <question>  |  PING  |  HELP
"""

import os
import re
import time
import logging
import datetime

import requests
import meshtastic
import meshtastic.serial_interface
from pubsub import pub
from google import genai
from dotenv import load_dotenv

# Charge automatiquement le .env à la racine du repo (remonte les dossiers
# parents depuis ce fichier jusqu'à le trouver). Mêmes clés que côté config
# Windows : GEMINI_API_KEY est utilisée ici, MESHBRIDGE_PSK ne l'est pas
# (le déchiffrement du canal se fait au niveau du firmware du nœud, pas ici).
load_dotenv()

# ----------------------------------------------------------------------
#  Configuration
# ----------------------------------------------------------------------
MAX_LEN            = 200                              # limite radio LoRa (octets)
MESHBRIDGE_CHANNEL = 1                                # canal privé ; on ignore LongFast (0)
HTTP_TIMEOUT       = 10
OLLAMA_URL         = "http://localhost:11434/api/generate"
OLLAMA_MODEL       = "llama3.2:1b"
GEMINI_KEY         = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL       = "gemini-3.5-flash"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("meshbridge")


# ----------------------------------------------------------------------
#  Utilitaires
# ----------------------------------------------------------------------
def trim(text: str, limit: int = MAX_LEN) -> str:
    """Coupe proprement sur les octets (pas les caractères) pour LoRa."""
    text = " ".join(text.split())
    if len(text.encode("utf-8")) <= limit:
        return text
    encoded = text.encode("utf-8")[: limit - 3]      # 3 octets réservés pour "…"
    return encoded.decode("utf-8", errors="ignore") + "…"


# ----------------------------------------------------------------------
#  Couche IA — cascade cloud → local → brut
# ----------------------------------------------------------------------
def summarize_cloud(content: str, instruction: str) -> str | None:
    """Compression via l'API Gemini. None si indisponible."""
    if not GEMINI_KEY:
        return None
    try:
        client = genai.Client(api_key=GEMINI_KEY)
        prompt = (f"{instruction}\nRéponds en moins de 180 caractères, "
                  f"sans préambule.\n\n---\n{content[:6000]}")
        resp = client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
        log.info("IA: cloud (Gemini) ✓")
        return resp.text.strip()
    except Exception as e:
        log.warning(f"IA cloud indisponible : {e}")
        return None


def summarize_local(content: str, instruction: str) -> str | None:
    """Compression via Ollama local. None si indisponible."""
    try:
        r = requests.post(
            OLLAMA_URL,
            json={
                "model": OLLAMA_MODEL,
                "prompt": (f"{instruction}\nRéponds en moins de 180 caractères, "
                           f"sans préambule.\n\n---\n{content[:4000]}"),
                "stream": False,
            },
            timeout=60,
        )
        r.raise_for_status()
        log.info("IA: local (Ollama) ✓")
        return r.json()["response"].strip()
    except Exception as e:
        log.warning(f"IA local indisponible : {e}")
        return None


def compress(content: str, instruction: str) -> str:
    """Cœur de la compression sémantique : cloud, sinon local, sinon brut."""
    result = summarize_cloud(content, instruction)
    if result is None:
        result = summarize_local(content, instruction)
    if result is None:
        log.warning("IA: fallback brut (troncature)")
        result = content
    return trim(result)


# ----------------------------------------------------------------------
#  Commandes
# ----------------------------------------------------------------------
def cmd_meteo(arg: str) -> str:
    ville = arg.strip() or "Geneve"
    try:
        url = f"https://wttr.in/{ville}?format=%l:+%c+%t+%w+%p&m"
        r = requests.get(url, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        return trim(r.text.strip())
    except Exception as e:
        log.error(f"METEO erreur : {e}")
        return f"Erreur meteo pour {ville}"


def cmd_news(arg: str) -> str:
    try:
        ids = requests.get(
            "https://hacker-news.firebaseio.com/v0/topstories.json",
            timeout=HTTP_TIMEOUT,
        ).json()[:5]
        titres = []
        for sid in ids:
            item = requests.get(
                f"https://hacker-news.firebaseio.com/v0/item/{sid}.json",
                timeout=HTTP_TIMEOUT,
            ).json()
            titres.append(item.get("title", ""))
        raw = " | ".join(titres)
        return compress(raw, "Résume ces titres d'actualité en français, format [1]...[2]...[3].")
    except Exception as e:
        log.error(f"NEWS erreur : {e}")
        return "Erreur news"


def cmd_web(arg: str) -> str:
    url = arg.strip()
    if not url:
        return "Usage: WEB <url>"
    if not url.startswith("http"):
        url = "https://" + url
    try:
        r = requests.get(url, timeout=HTTP_TIMEOUT,
                         headers={"User-Agent": "MeshBridge/1.0"})
        r.raise_for_status()
        text = re.sub(r"<script.*?</script>", " ", r.text, flags=re.S | re.I)
        text = re.sub(r"<style.*?</style>", " ", text, flags=re.S | re.I)
        text = re.sub(r"<[^>]+>", " ", text)
        text = " ".join(text.split())
        return compress(text, f"Résume l'essentiel de cette page web ({url}).")
    except Exception as e:
        log.error(f"WEB erreur : {e}")
        return f"Erreur acces {url}"


def cmd_ask(arg: str) -> str:
    q = arg.strip()
    if not q:
        return "Usage: ASK <question>"
    date_actuelle = datetime.datetime.now().strftime("%d %B %Y")
    instruction = (f"Nous sommes le {date_actuelle}. "
                   f"Réponds à cette question de façon factuelle et concise.")
    return compress(q, instruction)


def cmd_ping(arg: str) -> str:
    return "pong ✅ relai actif"


def cmd_help(arg: str) -> str:
    return "Cmds: METEO <v> | NEWS | WEB <url> | ASK <q> | PING | HELP"


COMMANDS = {
    "METEO": cmd_meteo,
    "NEWS":  cmd_news,
    "WEB":   cmd_web,
    "ASK":   cmd_ask,
    "PING":  cmd_ping,
    "HELP":  cmd_help,
}


def process(message: str) -> str:
    parts = message.strip().split(maxsplit=1)
    if not parts:
        return "Message vide. Tape HELP."
    verb = parts[0].upper()
    arg = parts[1] if len(parts) > 1 else ""
    handler = COMMANDS.get(verb)
    if handler is None:
        return f"Commande inconnue '{verb}'. Tape HELP."
    log.info(f"Exécution : {verb} {arg!r}")
    return handler(arg)


# ----------------------------------------------------------------------
#  Réseau LoRa
# ----------------------------------------------------------------------
def on_receive(packet, interface):
    try:
        # Anti-boucle : on ignore nos propres messages (on diffuse sur le canal)
        if packet.get("from") == interface.myInfo.my_node_num:
            return

        decoded = packet.get("decoded", {})
        if decoded.get("portnum") != "TEXT_MESSAGE_APP":
            return

        # On ne traite QUE le canal privé MeshBridge
        channel = packet.get("channel", 0)
        if channel != MESHBRIDGE_CHANNEL:
            return

        message = decoded.get("text", "").strip()
        sender = packet.get("fromId", "inconnu")
        if not message:
            return

        log.info(f"[IN]  ch{channel} {sender} → \"{message}\"")
        t0 = time.time()
        response = process(message)
        dt = time.time() - t0

        # Option A : DIFFUSION sur le canal (pas de destinationId) →
        # question et réponse dans le même fil, sans ACK ni retransmission.
        interface.sendText(response, channelIndex=channel)

        n = len(response.encode("utf-8"))
        log.info(f"[OUT] ch{channel} : \"{response}\" ({n} octets, {dt:.1f}s)")

    except Exception as e:
        log.error(f"Erreur de traitement : {e}")


def main():
    log.info("═══ MeshBridge — relai off-grid LoRa + IA ═══")
    cloud = "Gemini ✓" if GEMINI_KEY else "Gemini ✗ (pas de clé)"
    log.info(f"IA : {cloud}  |  Ollama {OLLAMA_MODEL} (fallback local)")

    try:
        iface = meshtastic.serial_interface.SerialInterface()
    except Exception as e:
        log.error(f"Connexion au nœud impossible : {e}")
        return

    log.info(f"Nœud local : {iface.myInfo.my_node_num}")
    log.info(f"Écoute + réponse sur le canal {MESHBRIDGE_CHANNEL} (MeshBridge privé)")
    pub.subscribe(on_receive, "meshtastic.receive.text")
    log.info("Prêt. Commandes : METEO / NEWS / WEB / ASK / PING / HELP")

    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        log.info("Arrêt propre.")
    finally:
        iface.close()


if __name__ == "__main__":
    main()
