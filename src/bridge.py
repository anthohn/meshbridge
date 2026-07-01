#!/usr/bin/env python3
"""
MeshBridge — Point d'entrée (Raspberry Pi), interface BLE.
==========================================================
Aurora (Heltec) est placé contre la fenêtre pour maximiser la portée LoRa,
et parle au Pi en Bluetooth Low Energy — pas d'USB à tirer.

Architecture :
  - couche mesh : BLEInterface, réouverte automatiquement si elle tombe ;
  - callback radio non bloquant : enfile la commande et rend la main ;
  - worker : traite les requêtes web/IA (30-60 s) sans figer la réception ;
  - verrou d'émission : un seul writer BLE à la fois.

Lancer :  python3 src/bridge.py   (le .env est chargé automatiquement)
"""
import time
import queue
import logging
import threading

import meshtastic
import meshtastic.ble_interface
from pubsub import pub

import config
from commands import dispatch, SLOW

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("meshbridge")

# File d'attente des commandes + verrou d'émission (un seul writer à la fois)
_tasks = queue.Queue()
_send_lock = threading.Lock()

# Référence partagée vers l'interface active. Un dict pour pouvoir la
# remplacer depuis main() sans casser les closures du callback/worker.
_iface = {"handle": None}

# Événement levé par pubsub quand la connexion tombe → réveille main()
_connection_lost = threading.Event()


def safe_send(text: str, channel: int) -> None:
    """Émission BLE sérialisée. Ignore silencieusement si déco (main relance)."""
    iface = _iface["handle"]
    if iface is None:
        log.warning(f"envoi ignoré (pas d'interface) : {text[:40]!r}")
        return
    try:
        with _send_lock:
            iface.sendText(text, channelIndex=channel)
    except Exception as e:
        log.error(f"sendText a échoué : {e}")
        _connection_lost.set()


def worker() -> None:
    """Traite les commandes en tâche de fond, sans bloquer la réception."""
    while True:
        channel, verb, arg = _tasks.get()
        try:
            t0 = time.time()
            response = dispatch(verb, arg)
            dt = time.time() - t0
            safe_send(response, channel)
            n = len(response.encode("utf-8"))
            log.info(f"[OUT] ch{channel} /{verb} → {n} o en {dt:.1f}s")
        except Exception as e:
            log.error(f"worker : {e}")
        finally:
            _tasks.task_done()


def on_receive(packet, interface) -> None:
    """Callback radio : valide, enfile. Ne bloque jamais."""
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
        channel = packet.get("channel", config.MESHBRIDGE_CHANNEL)

        log.info(f"[IN]  ch{channel} → /{verb} {arg!r}")

        if config.SEND_ACK and verb in SLOW:
            safe_send(config.ACK_TEXT, channel)

        _tasks.put((channel, verb, arg))

    except Exception as e:
        log.error(f"on_receive : {e}")


def on_connection_lost(interface, topic=pub.AUTO_TOPIC) -> None:
    """Callback pubsub : Meshtastic signale une déconnexion BLE."""
    log.warning("[BLE] connexion perdue — reconnexion en cours…")
    _connection_lost.set()


def open_ble() -> meshtastic.ble_interface.BLEInterface | None:
    """Tente une seule fois d'ouvrir la liaison BLE avec Aurora."""
    try:
        log.info(f"[BLE] connexion à Aurora ({config.AURORA_BLE_MAC})…")
        iface = meshtastic.ble_interface.BLEInterface(config.AURORA_BLE_MAC)
        log.info(f"[BLE] connecté. Nœud local : {iface.myInfo.my_node_num}")
        return iface
    except Exception as e:
        log.error(f"[BLE] échec de connexion : {e}")
        return None


def main() -> None:
    log.info("═══ MeshBridge — relai off-grid LoRa + IA (BLE) ═══")

    if not config.AURORA_BLE_MAC:
        log.error("AURORA_BLE_MAC absent du .env — impossible de démarrer.")
        return

    log.info(f"IA cloud : {'Gemini ✓' if config.GEMINI_KEY else 'Gemini ✗ (local seul)'}"
             f"  |  IA local : Ollama {config.OLLAMA_MODEL}")
    log.info(f"Écoute + réponse sur le canal {config.MESHBRIDGE_CHANNEL} (MeshBridge privé)")

    # Callbacks pubsub (une seule fois, valides pour toutes les reconnexions)
    pub.subscribe(on_receive, "meshtastic.receive.text")
    pub.subscribe(on_connection_lost, "meshtastic.connection.lost")

    # Worker démarré une fois — il consommera la file quoi qu'il arrive
    threading.Thread(target=worker, daemon=True).start()

    # Boucle de reconnexion : retry exponentiel plafonné
    delay = config.BLE_RECONNECT_MIN_S
    try:
        while True:
            iface = open_ble()
            if iface is None:
                log.warning(f"[BLE] nouvel essai dans {delay}s")
                time.sleep(delay)
                delay = min(delay * 2, config.BLE_RECONNECT_MAX_S)
                continue

            # Connexion réussie : on remet le délai à son minimum
            _iface["handle"] = iface
            _connection_lost.clear()
            delay = config.BLE_RECONNECT_MIN_S
            log.info("Prêt. Commandes : /meteo /news /web /ask /ping /help")

            # On dort jusqu'à ce qu'une déconnexion soit signalée
            _connection_lost.wait()

            # Nettoyage propre avant de retenter
            log.info("[BLE] fermeture de l'interface avant reconnexion…")
            _iface["handle"] = None
            try:
                iface.close()
            except Exception:
                pass
            time.sleep(delay)

    except KeyboardInterrupt:
        log.info("Arrêt propre.")
    finally:
        if _iface["handle"] is not None:
            try:
                _iface["handle"].close()
            except Exception:
                pass


if __name__ == "__main__":
    main()