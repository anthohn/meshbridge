#!/usr/bin/env python3
"""
MeshBridge — Point d'entrée (Raspberry Pi).
===========================================
Relai Internet off-grid via LoRa + compression IA.

Architecture NON BLOQUANTE :
  - le callback radio ne fait qu'ENFILER la commande (rapide) ;
  - un thread worker traite les requêtes une par une (le web/IA peut
    prendre 30-60 s sans jamais figer la réception) ;
  - un accusé de réception immédiat est envoyé pour les commandes lentes.

Lancer :  python3 src/bridge.py   (le .env est chargé automatiquement)
"""
import time
import queue
import logging
import threading

import meshtastic
import meshtastic.serial_interface
from pubsub import pub

import config
from commands import dispatch, SLOW

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("meshbridge")

# File d'attente des commandes + verrou d'émission (un seul writer radio à la fois)
_tasks = queue.Queue()
_send_lock = threading.Lock()


def safe_send(interface, text: str, channel: int) -> None:
    """Émission série sérialisée (thread-safe)."""
    with _send_lock:
        interface.sendText(text, channelIndex=channel)


def worker(interface) -> None:
    """Traite les commandes en tâche de fond, sans bloquer la réception."""
    while True:
        channel, verb, arg = _tasks.get()
        try:
            t0 = time.time()
            response = dispatch(verb, arg)
            dt = time.time() - t0
            safe_send(interface, response, channel)
            n = len(response.encode("utf-8"))
            log.info(f"[OUT] ch{channel} /{verb} → {n} o en {dt:.1f}s")
        except Exception as e:
            log.error(f"worker : {e}")
        finally:
            _tasks.task_done()


def on_receive(packet, interface) -> None:
    """Callback radio : valide, accuse réception, enfile. Ne bloque jamais."""
    try:
        # Anti-boucle : ignore nos propres messages
        if packet.get("from") == interface.myInfo.my_node_num:
            return

        decoded = packet.get("decoded", {})
        if decoded.get("portnum") != "TEXT_MESSAGE_APP":
            return

        # Uniquement le canal privé MeshBridge
        if packet.get("channel", 0) != config.MESHBRIDGE_CHANNEL:
            return

        message = (decoded.get("text") or "").strip()
        if not message.startswith("/"):     # préfixe "/" obligatoire
            return

        parts = message[1:].strip().split(maxsplit=1)
        if not parts:
            return
        verb = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""
        channel = packet["channel"] if "channel" in packet else config.MESHBRIDGE_CHANNEL

        log.info(f"[IN]  ch{channel} → /{verb} {arg!r}")

        # Accusé de réception immédiat pour les commandes lentes
        if config.SEND_ACK and verb in SLOW:
            safe_send(interface, config.ACK_TEXT, channel)

        _tasks.put((channel, verb, arg))

    except Exception as e:
        log.error(f"on_receive : {e}")


def main() -> None:
    log.info("═══ MeshBridge — relai off-grid LoRa + IA ═══")
    log.info(f"IA cloud : {'Gemini ✓' if config.GEMINI_KEY else 'Gemini ✗ (local seul)'}"
             f"  |  IA local : Ollama {config.OLLAMA_MODEL}")

    try:
        iface = meshtastic.serial_interface.SerialInterface()
    except Exception as e:
        log.error(f"Connexion au nœud impossible : {e}")
        return

    log.info(f"Nœud local : {iface.myInfo.my_node_num}")
    log.info(f"Écoute + réponse sur le canal {config.MESHBRIDGE_CHANNEL} (MeshBridge privé)")

    threading.Thread(target=worker, args=(iface,), daemon=True).start()
    pub.subscribe(on_receive, "meshtastic.receive.text")
    log.info("Prêt. Commandes : /meteo /news /web /ask /ping /help")

    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        log.info("Arrêt propre.")
    finally:
        iface.close()


if __name__ == "__main__":
    main()
