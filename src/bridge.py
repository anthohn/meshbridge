#!/usr/bin/env python3
"""
MeshBridge — Point d'entrée (Raspberry Pi), interface BLE.
==========================================================
Pierre (Heltec) est placé contre la fenêtre pour maximiser la portée LoRa,
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
from commands import dispatch, SLOW, COMMANDS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("meshbridge")

# File d'attente des commandes + verrou d'émission (un seul writer à la fois).
# Bornée : voir config.QUEUE_MAX (anti-flood, budget duty cycle).
_tasks = queue.Queue(maxsize=config.QUEUE_MAX)
_send_lock = threading.Lock()

# Référence partagée vers l'interface active. Un dict pour pouvoir la
# remplacer depuis main() sans casser les closures du callback/worker.
_iface = {"handle": None}

# Événement levé par pubsub quand la connexion tombe → réveille main()
_connection_lost = threading.Event()

# Dernière réponse non émise (BLE tombé pendant l'envoi) : un seul slot,
# la plus récente gagne. main() la rejoue après reconnexion — une réponse
# a coûté 30-60 s d'IA, la perdre obligerait l'utilisateur à tout refaire.
_unsent = {"reply": None}


def safe_send(text: str, dest: int) -> bool:
    """Émission BLE sérialisée, en DM chiffré (PKC) vers le nœud `dest`.
    Renvoie False si l'envoi a échoué (déco)."""
    iface = _iface["handle"]
    if iface is None:
        log.warning(f"envoi ignoré (pas d'interface) : {text[:40]!r}")
        return False
    try:
        with _send_lock:
            iface.sendText(text, destinationId=dest, pkiEncrypted=True)
        return True
    except Exception as e:
        log.error(f"sendText a échoué : {e}")
        _connection_lost.set()
        return False


def worker() -> None:
    """Traite les commandes en tâche de fond, sans bloquer la réception."""
    while True:
        dest, verb, arg = _tasks.get()
        try:
            t0 = time.time()
            response = dispatch(verb, arg)
            dt = time.time() - t0
            if safe_send(response, dest):
                n = len(response.encode("utf-8"))
                log.info(f"[OUT] DM /{verb} → {n} o en {dt:.1f}s")
            else:
                _unsent["reply"] = (response, dest)
                log.warning(f"[OUT] /{verb} : BLE indisponible — réponse mise en attente")
        except Exception as e:
            log.error(f"worker : {e}")
        finally:
            _tasks.task_done()


def authentic_sender(packet, interface) -> int | None:
    """Numéro de l'émetteur si le paquet est un DM signé par Paul, sinon None.
    Trois garde-fous, du plus simple au plus fort :
      1. le DM nous est adressé (to == nous) : ce n'est pas un broadcast ;
      2. il est chiffré/authentifié par le firmware (pkiEncrypted) ;
      3. il vient de Paul ET la clé publique vue par Pierre correspond à
         celle épinglée dans le .env (barrière anti-usurpation)."""
    if packet.get("to") != interface.myInfo.my_node_num:
        return None
    if not packet.get("pkiEncrypted"):
        return None
    sender = packet.get("from")
    if sender != config.PAUL_NODE_NUM:
        return None
    node = (interface.nodesByNum or {}).get(sender, {})
    seen_key = node.get("user", {}).get("publicKey")
    if not config.PAUL_PUBLIC_KEY or seen_key != config.PAUL_PUBLIC_KEY:
        log.warning(f"[AUTH] clé de Paul inattendue (vue={seen_key!r}) — DM ignoré")
        return None
    return sender


def on_receive(packet, interface) -> None:
    """Callback radio : valide, enfile. Ne bloque jamais."""
    try:
        decoded = packet.get("decoded", {})
        if decoded.get("portnum") != "TEXT_MESSAGE_APP":
            return

        # On ne parle qu'à Paul, en DM chiffré. Tout le reste (broadcast,
        # DM non signé, autre nœud) est ignoré en silence — pas d'oracle.
        sender = authentic_sender(packet, interface)
        if sender is None:
            return

        message = (decoded.get("text") or "").strip()
        if not message.startswith("/"):     # préfixe "/" obligatoire
            return

        parts = message[1:].strip().split(maxsplit=1)
        if not parts:
            return
        verb = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        log.info(f"[IN]  DM Paul → /{verb} {arg!r}")

        try:
            _tasks.put_nowait((sender, verb, arg))
        except queue.Full:
            # On jette sans répondre : répondre à un flood amplifierait
            # le trafic radio (duty cycle). L'événement reste loggé.
            log.warning(f"file pleine ({config.QUEUE_MAX}) — /{verb} ignoré")
            return

        # ACK seulement si la commande est réellement en file
        if config.SEND_ACK and verb in SLOW:
            safe_send(config.ACK_TEXT, sender)

    except Exception as e:
        log.error(f"on_receive : {e}")


def on_connection_lost(interface, topic=pub.AUTO_TOPIC) -> None:
    """Callback pubsub : Meshtastic signale une déconnexion BLE.

    On ne réagit qu'à la déconnexion de l'interface *active*. Une
    tentative de connexion ratée (nœud absent au démarrage) et la
    fermeture volontaire pendant la reconnexion émettent le même
    événement, de façon différée : sans ce filtre, ces signaux
    parasites démontent en boucle la connexion tout juste rétablie
    (bug « nœud allumé après le Pi »). Chaque BLEInterface étant un
    objet unique, comparer à _iface["handle"] suffit à les écarter.
    """
    if interface is not _iface["handle"]:
        return
    log.warning("[BLE] connexion perdue — reconnexion en cours…")
    _connection_lost.set()


def open_ble() -> meshtastic.ble_interface.BLEInterface | None:
    """Tente une seule fois d'ouvrir la liaison BLE avec Pierre."""
    try:
        log.info(f"[BLE] connexion à Pierre ({config.PIERRE_BLE_MAC})…")
        iface = meshtastic.ble_interface.BLEInterface(config.PIERRE_BLE_MAC)
        log.info(f"[BLE] connecté. Nœud local : {iface.myInfo.my_node_num}")
        return iface
    except Exception as e:
        log.error(f"[BLE] échec de connexion : {e}")
        return None


def main() -> None:
    log.info("═══ MeshBridge — relai off-grid LoRa + IA (BLE) ═══")

    if not config.PIERRE_BLE_MAC:
        log.error("PIERRE_BLE_MAC absent du .env — impossible de démarrer.")
        return

    log.info(f"IA cloud : {'Gemini ✓' if config.GEMINI_KEY else 'Gemini ✗ (local seul)'}"
             f"  |  IA local : Ollama {config.OLLAMA_MODEL}")
    if config.PAUL_NODE_NUM is None or not config.PAUL_PUBLIC_KEY:
        log.warning("PAUL_NODE_ID / PAUL_PUBLIC_KEY absent du .env — aucune commande ne sera acceptée")
    else:
        log.info(f"Écoute des DM chiffrés de Paul (nœud {config.PAUL_NODE_NUM:08x})")

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
            # Liste générée depuis le registre : toujours à jour
            log.info("Prêt. Commandes : " + " ".join("/" + n for n in COMMANDS))

            # Rejoue l'éventuelle réponse restée en rade pendant la coupure.
            # (Si l'envoi échoue encore, _connection_lost est relevé et le
            # slot est conservé pour le prochain cycle.)
            pending = _unsent["reply"]
            if pending and safe_send(*pending):
                _unsent["reply"] = None
                log.info("[BLE] réponse en attente renvoyée")

            # On dort jusqu'à ce qu'une déconnexion soit signalée
            _connection_lost.wait()

            # Nettoyage propre avant de retenter. On détache le handle
            # AVANT close() : la fermeture émet un connection.lost différé
            # que on_connection_lost doit ignorer (interface != handle
            # courant, désormais None). Ne pas inverser ces deux lignes.
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