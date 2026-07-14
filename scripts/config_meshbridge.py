#!/usr/bin/env python3
"""
MeshBridge — Assistant de configuration des nœuds Meshtastic
Normes Netiquette Suisse (janvier 2026) + canal privé chiffré.

Fonctionnement : le script détecte le nœud branché en USB et guide
pas à pas. Un nœud vierge se voit attribuer un rôle (Pierre fixe /
Paul portable) ; un nœud déjà nommé est vérifié en priorité.
Chaque paramètre critique est relu après écriture et comparé à la
valeur attendue, y compris la table des canaux — le succès n'est déclaré
que si TOUS les contrôles passent (10/10 pour un nœud MeshBridge).
"""

import os
import sys
import subprocess
import re
import shutil
import time
import getpass
import hashlib
from dotenv import load_dotenv

# ======================================================================
#  CHARGEMENT DU .env (à la racine du repo, à côté de README.md)
# ======================================================================
script_dir = os.path.dirname(os.path.abspath(__file__))
root_dir = os.path.dirname(script_dir)
env_path = os.path.join(root_dir, ".env")

if os.path.exists(env_path):
    load_dotenv(dotenv_path=env_path)
    print(f"\033[90m[.env] Variables chargées depuis {env_path}\033[0m")
else:
    print(f"\033[93m[!] .env introuvable à la racine du repo ({env_path})\033[0m")
    print("\033[93m    Copier .env.example en .env et le compléter.\033[0m")

# ======================================================================
#  CONFIGURATION GLOBALE
# ======================================================================
CONFIG = {
    "PSK": os.environ.get("MESHBRIDGE_PSK"),
    "Channel": 1,         # index du canal privé MeshBridge
    "MinFirmware": "2.7.0",   # version minimale recommandée

    # Noms NEUTRES diffusés dans le NodeInfo public (toutes les 3h, en clair).
    "Nodes": {
        "Maison": {"Long": "Pierre", "Short": "PIE"},
        "Portable": {"Long": "Paul", "Short": "PAU"}
    }
}

# Valeurs pour traduire les codes en libellés lisibles lors de la vérification.
ENUMS = {
    "lora.region": {"3": "EU_868"},
    "lora.modem_preset": {"0": "LONG_FAST", "4": "MEDIUM_FAST"},
    "device.role": {"0": "CLIENT", "1": "CLIENT_MUTE"},
    "device.rebroadcast_mode": {"0": "ALL", "2": "LOCAL_ONLY"}
}

# ======================================================================
#  UTILITAIRES
# ======================================================================

def write_step(m):
    print(f"  \033[90m-> {m}\033[0m")

def write_ok(m):
    print(f"  \033[92m[OK]\033[0m   {m}")

def write_fail(m):
    print(f"  \033[91m[ÉCHEC]\033[0m {m}")

def write_title(m):
    print(f"\n\033[96m{m}\033[0m")

def ask_yes_no(question, default=True):
    """Question fermée O/n. Entrée seule = valeur par défaut."""
    rep = input(f"{question} [{'O/n' if default else 'o/N'}] : ").strip().lower()
    if not rep:
        return default
    return rep in ("o", "oui", "y", "yes")

def invoke_meshtastic(args):
    """Exécute la CLI meshtastic, capture stdout+stderr, avec tentatives de
    reconnexion automatique en cas de déconnexion/reboot du nœud."""
    max_retries = 15
    retry_delay = 2

    for attempt in range(1, max_retries + 1):
        try:
            result = subprocess.run(["meshtastic"] + args, capture_output=True, text=True, check=True)
            return result.stdout + result.stderr
        except subprocess.CalledProcessError as e:
            output = e.stdout + e.stderr if (e.stdout or e.stderr) else str(e)

            # Mots-clés indiquant que le nœud est hors-ligne / en cours de reboot
            connection_keywords = [
                "no serial meshtastic device detected",
                "connection refused",
                "error connecting",
                "no meshtastic device found",
                "serialexception",
                "device not found",
                "timed out"
            ]
            if any(kw in output.lower() for kw in connection_keywords):
                if attempt == 1:
                    # Affiche la cause réelle dès le premier échec, sinon on
                    # retente 30 s en aveugle (vécu : ModemManager, dialout…)
                    kw_hit = next(kw for kw in connection_keywords if kw in output.lower())
                    ligne = next((l.strip() for l in output.splitlines()
                                  if kw_hit in l.lower()), kw_hit)
                    print(f"  \033[90m(cause : {ligne[:90]})\033[0m")
                if attempt < max_retries:
                    print(f"  \033[90m[!] Nœud hors-ligne (reboot ?). Nouvel essai dans {retry_delay}s... ({attempt}/{max_retries})\033[0m")
                    time.sleep(retry_delay)
                    continue
            raise RuntimeError(f"meshtastic {' '.join(args)} a échoué (code {e.returncode}) :\n{output}")
        except FileNotFoundError:
            raise RuntimeError("La CLI 'meshtastic' est introuvable. L'installer avec : pip install meshtastic")

    raise RuntimeError(f"Impossible de joindre le nœud après {max_retries} tentatives pour la commande : meshtastic {' '.join(args)}")

def get_setting(field):
    """Lit un champ de config et renvoie sa valeur brute."""
    raw = invoke_meshtastic(["--get", field])
    for line in raw.splitlines():
        match = re.match(rf"^\s*{re.escape(field)}\s*:\s*(.+?)\s*$", line, re.IGNORECASE)
        if match:
            return match.group(1).strip()
    raise RuntimeError(f"Impossible de lire la valeur de '{field}'.")

def test_setting(field, expected, label=None):
    """Vérifie qu'un champ vaut bien la valeur attendue."""
    if label is None:
        label = field

    try:
        actual = get_setting(field)
    except Exception as e:
        write_fail(f"{label:<22} = ERREUR (impossible de lire : {e})")
        return False

    actual_label = actual
    if field in ENUMS and actual in ENUMS[field]:
        actual_label = ENUMS[field][actual]

    if actual_label == expected or actual == expected:
        write_ok(f"{label:<22} = {actual_label}")
        return True
    else:
        write_fail(f"{label:<22} = {actual_label}  (attendu : {expected})")
        return False

# ======================================================================
#  DÉTECTION USB
# ======================================================================

def find_ports():
    """Liste les ports série des nœuds Meshtastic branchés en USB.
    Utilise la détection officielle de la lib meshtastic (mêmes critères
    que la CLI elle-même)."""
    try:
        import meshtastic.util
        return meshtastic.util.findPorts(True)
    except ImportError:
        write_fail("Le paquet Python 'meshtastic' est requis : pip install meshtastic")
        sys.exit(1)

def wait_for_single_node():
    """Attend qu'exactement UN nœud soit branché en USB, puis le renvoie.
    Vérifie aussi le droit d'accès au port : sans lui, tout échouerait
    plus loin avec des messages trompeurs."""
    while True:
        ports = find_ports()
        if len(ports) == 1:
            port = ports[0]
            if not os.access(port, os.R_OK | os.W_OK):
                write_fail(f"Pas le droit d'accéder à {port}.")
                user = getpass.getuser()
                print(f"\033[93m  Ajouter l'utilisateur au groupe 'dialout', puis se déconnecter/reconnecter :\033[0m")
                print(f"\033[93m    sudo usermod -a -G dialout {user}\033[0m")
                print(f"\033[93m  Ou, pour essayer sans se déconnecter :\033[0m")
                print(f"\033[93m    sg dialout -c \"python3 {sys.argv[0]}\"\033[0m")
                sys.exit(1)
            write_ok(f"Nœud détecté sur {port}")
            return port
        if not ports:
            input("\n\033[93mAucun nœud détecté. Brancher un nœud en USB, puis Entrée pour re-scanner…\033[0m ")
        else:
            print(f"\n\033[93m{len(ports)} nœuds détectés ({', '.join(ports)}).\033[0m")
            input("\033[93mUn seul nœud peut être configuré à la fois. En débrancher un, puis Entrée…\033[0m ")

def probe_node():
    """Interroge le nœud une seule fois (20 s max) pour savoir s'il parle
    Meshtastic. Renvoie (True, version, nom) s'il répond, (False, None, None)
    sinon — sans chercher à deviner quel autre firmware tourne : un handshake
    muet peut venir d'un firmware étranger (MeshCore) comme d'un boot en cours,
    et le message d'aide couvre les deux cas."""
    try:
        r = subprocess.run(["meshtastic", "--info"], capture_output=True,
                           text=True, timeout=20)
        out = r.stdout + r.stderr
        if r.returncode == 0 and "Owner" in out:
            version = None
            m = re.search(r'firmware_?version[^0-9]*(\d+\.\d+\.\d+)', out, re.I)
            if m:
                version = m.group(1)
            owner = None
            m = re.search(r"^Owner\s*:\s*(.+)$", out, re.M)
            if m:
                owner = re.sub(r"\s*\(.*\)$", "", m.group(1)).strip()  # "Pierre (PIE)" → "Pierre"
            return (True, version, owner)
    except subprocess.TimeoutExpired:
        pass
    except Exception as e:
        write_fail(f"Sonde impossible : {e}")
    return (False, None, None)


def latest_stable_firmware():
    """Dernière version stable Meshtastic (API GitHub). None si hors-ligne."""
    try:
        import json
        import urllib.request
        url = "https://api.github.com/repos/meshtastic/firmware/releases/latest"
        with urllib.request.urlopen(url, timeout=5) as r:
            tag = json.load(r).get("tag_name", "")
        m = re.search(r"(\d+\.\d+\.\d+)", tag)
        return m.group(1) if m else None
    except Exception:
        return None

# ======================================================================
#  PRÉREQUIS
# ======================================================================

def test_prerequisites():
    write_title("Vérification des prérequis...")

    if not CONFIG["PSK"] or not CONFIG["PSK"].strip():
        write_fail("MESHBRIDGE_PSK manquant ou vide.")
        print("\033[93m  Vérifier que .env existe à la racine du repo et contient MESHBRIDGE_PSK=...\033[0m")
        sys.exit(1)

    if not shutil.which("meshtastic"):
        write_fail("CLI 'meshtastic' introuvable. L'installer : pip install meshtastic")
        sys.exit(1)

    try:
        raw_version = invoke_meshtastic(["--version"])
        version = raw_version.splitlines()[0].strip()
        write_ok(f"CLI meshtastic {version}")
    except Exception as e:
        write_fail(f"La CLI meshtastic ne répond pas :\n{e}")
        sys.exit(1)

# ======================================================================
#  APPLICATION DE LA CONFIGURATION
# ======================================================================

def set_common_settings(long_name, short_name, etape="[1/3]"):
    write_title(f"{etape} Identité et paramètres communs ({long_name} / {short_name})")

    write_step("Écriture des informations propriétaires...")
    invoke_meshtastic(["--set-owner", long_name, "--set-owner-short", short_name])
    time.sleep(3) # pause pour laisser le nœud initier son reboot si nécessaire

    write_step("Écriture LoRa / réseau suisse / télémétrie...")
    invoke_meshtastic([
        "--set", "lora.region", "EU_868",
        "--set", "lora.use_preset", "true",
        "--set", "lora.hop_limit", "3",
        "--set", "lora.override_duty_cycle", "false",
        "--set", "lora.ignore_mqtt", "true",
        "--set", "device.node_info_broadcast_secs", "10800",
        "--set", "position.position_broadcast_smart_enabled", "false",
        "--set", "telemetry.device_update_interval", "259200",
        "--set", "telemetry.environment_measurement_enabled", "false",
        "--set", "telemetry.power_measurement_enabled", "false",
        "--set", "mqtt.enabled", "false"
    ])
    time.sleep(3)

    # Preset appliqué SÉPARÉMENT
    write_step("Application du preset MEDIUM_FAST (transaction isolée)...")
    invoke_meshtastic(["--set", "lora.modem_preset", "MEDIUM_FAST"])
    time.sleep(3)

def set_role_settings(node_type):
    if node_type == "Maison":
        write_title("[2/3] Règles MAISON (fixe / position 24h)")
        pos_secs = "86400"
        fixed = "true"
    else:
        write_title("[2/3] Règles PORTABLE (mobile / position 6h)")
        pos_secs = "21600"
        fixed = "false"

    # Position d'abord : le changement de rôle (ci-dessous) fait rebooter
    # le nœud, ce qui perdait les écritures envoyées après lui dans le
    # même lot (vu en conditions réelles : 6/7, position non appliquée).
    write_step("Écriture des réglages de position...")
    invoke_meshtastic([
        "--set", "position.position_broadcast_secs", pos_secs,
        "--set", "position.fixed_position", fixed
    ])
    time.sleep(3)

    write_step("Écriture du rôle (peut faire rebooter le nœud)...")
    invoke_meshtastic([
        "--set", "device.role", "CLIENT_MUTE",
        "--set", "device.rebroadcast_mode", "LOCAL_ONLY"
    ])
    time.sleep(3)

def set_public_primary():
    """Canal 0 = canal public suisse par défaut. INDISPENSABLE pour la portée :
    la fréquence LoRa dérive du nom du canal primary, donc être sur le public
    met le nœud sur la même fréquence que le mesh suisse → les autres nœuds
    relaient son trafic (rebonds). Un primary custom isolerait le nœud."""
    write_step("Canal 0 remis au public suisse (fréquence du mesh, rebonds)...")
    invoke_meshtastic([
        "--ch-index", "0",
        "--ch-set", "psk", "default",
        "--ch-set", "name", ""
    ])
    time.sleep(3)


def purge_extra_channels():
    """Supprime les canaux résiduels (index ≥ 2). Un flash de firmware en
    mode « update » ne nettoie PAS la config : les vieux canaux survivent.
    Le déploiement doit aboutir à un état exact — 0 public + 1 MeshBridge,
    rien d'autre. Suppression du plus haut index vers le bas."""
    try:
        info = invoke_meshtastic(["--info"])
    except Exception as e:
        write_fail(f"Lecture des canaux impossible : {e}")
        return
    residuels = sorted({int(m.group(1))
                        for m in re.finditer(r"Index (\d+): SECONDARY", info)
                        if int(m.group(1)) >= 2}, reverse=True)
    for i in residuels:
        write_step(f"Suppression du canal résiduel (index {i})...")
        invoke_meshtastic(["--ch-index", str(i), "--ch-del"])
        time.sleep(2)


def reset_nodedb():
    """Vide la liste des nœuds connus (NodeDB). Après un changement de
    canal/fréquence, les anciennes entrées ne reflètent plus ce que le
    nœud entend réellement — repartir de zéro rend la liste fiable.
    Elle se repeuple automatiquement à l'écoute du mesh."""
    write_step("Réinitialisation de la liste des nœuds connus (NodeDB)...")
    invoke_meshtastic(["--reset-nodedb"])
    time.sleep(3)


def set_private_channel():
    write_title("[3/3] Canaux : public (0, portée) + MeshBridge privé (1, chiffré)")

    set_public_primary()

    # Canal 1 = MeshBridge chiffré. Les relais publics transportent ces paquets
    # sans pouvoir les lire (ils n'ont pas le PSK) : portée ET confidentialité.
    write_step("Canal MeshBridge (secondaire, chiffré)...")
    invoke_meshtastic([
        "--ch-index", str(CONFIG["Channel"]),
        "--ch-set", "name", "MeshBridge",
        "--ch-set", "psk", f"base64:{CONFIG['PSK']}",
        "--ch-set", "uplink_enabled", "false",
        "--ch-set", "downlink_enabled", "false"
    ])
    time.sleep(5) # Pause plus longue après la configuration finale des canaux avant vérification

    purge_extra_channels()

# ======================================================================
#  VÉRIFICATION POST-DÉPLOIEMENT
# ======================================================================

def test_channels(strict=True):
    """Vérifie la table des canaux (une seule lecture --info) et renvoie
    une LISTE de résultats, un par contrôle :
      - canal 0 = public par défaut (fixe la fréquence du mesh → rebonds) ;
      - en mode strict (nœuds MeshBridge) : canal 1 = « MeshBridge » et
        AUCUN canal résiduel au-delà.
    Règle : tout ce que le déploiement garantit, la vérification doit le
    contrôler — sinon un nœud « conforme en apparence » (p. ex. canaux
    fantômes survivant à un flash « update ») ne déclenche jamais le
    re-déploiement qui l'aurait réparé (vécu)."""
    try:
        info = invoke_meshtastic(["--info"])
    except Exception as e:
        write_fail(f"{'Canaux':<22} = ERREUR (illisible : {e})")
        return [False]

    resultats = []

    # Canal 0 : primary public (nom vide + clé par défaut)
    ligne0 = next((l for l in info.splitlines() if "Index 0: PRIMARY" in l), None)
    if ligne0 is None:
        write_fail(f"{'Canal 0':<22} = introuvable dans --info")
        resultats.append(False)
    else:
        m = re.search(r'"name":\s*"([^"]*)"', ligne0)
        nom = m.group(1) if m else ""
        if nom == "" and "psk=secret" not in ligne0:
            write_ok(f"{'Canal 0':<22} = public (défaut)")
            resultats.append(True)
        else:
            write_fail(f"{'Canal 0':<22} = personnalisé « {nom or 'clé custom'} »  (attendu : public)")
            resultats.append(False)

    if not strict:
        return resultats

    # Canal 1 : MeshBridge
    ligne1 = next((l for l in info.splitlines() if "Index 1: SECONDARY" in l), None)
    if ligne1 and '"name": "MeshBridge"' in ligne1:
        write_ok(f"{'Canal 1':<22} = MeshBridge")
        resultats.append(True)
    else:
        write_fail(f"{'Canal 1':<22} = absent ou différent  (attendu : MeshBridge)")
        resultats.append(False)

    # Canaux résiduels (index ≥ 2) : la purge du déploiement doit les avoir ôtés
    residuels = sorted({int(m.group(1))
                        for m in re.finditer(r"Index (\d+): SECONDARY", info)
                        if int(m.group(1)) >= 2})
    if residuels:
        write_fail(f"{'Canaux résiduels':<22} = index {residuels}  (attendu : aucun)")
        resultats.append(False)
    else:
        write_ok(f"{'Canaux résiduels':<22} = aucun")
        resultats.append(True)

    return resultats


def test_deployment(pos_expected, rebroadcast="LOCAL_ONLY", role="CLIENT_MUTE",
                    strict_channels=True):
    """Relit les champs critiques : 7 réglages + la table des canaux.
    `rebroadcast`/`role` : LOCAL_ONLY/CLIENT_MUTE pour les nœuds MeshBridge,
    variables pour un nœud standard. `strict_channels` : True pour MeshBridge
    (canal 1 + absence de résiduels contrôlés), False pour un nœud standard
    (seul le canal 0 public est exigé, ses canaux perso ne nous regardent pas)."""
    write_title("Vérification (relecture réelle des champs)")

    results = [
        test_setting("lora.region", "EU_868", "Région"),
        test_setting("lora.modem_preset", "MEDIUM_FAST", "Preset LoRa"),
        test_setting("lora.hop_limit", "3", "Hop limit"),
        test_setting("device.role", role, "Rôle"),
        test_setting("device.rebroadcast_mode", rebroadcast, "Rebroadcast"),
        test_setting("position.position_broadcast_smart_enabled", "False", "Smart position"),
        test_setting("position.position_broadcast_secs", pos_expected, "Position interval"),
    ]
    results += test_channels(strict_channels)

    passed = sum(1 for r in results if r)
    total = len(results)

    print("")
    if passed == total:
        print(f"  \033[92m✅ {passed}/{total} vérifications réussies — nœud conforme.\033[0m")
        return True
    else:
        print(f"  \033[91m❌ {passed}/{total} réussies — {total - passed} à corriger ci-dessus.\033[0m")
        return False

def show_channel_fingerprint():
    """Affiche une empreinte du canal, PAS l'URL complète : celle-ci
    contient le PSK en clair (danger dans un screenshot ou un rapport)."""
    write_title("Empreinte du canal (doit être IDENTIQUE sur les deux nœuds)")
    info = invoke_meshtastic(["--info"])
    for line in info.splitlines():
        if "Complete URL" in line:
            match = re.search(r"https://\S+", line)
            if match:
                empreinte = hashlib.sha256(match.group(0).encode()).hexdigest()[:12]
                print(f"  Empreinte : \033[97m{empreinte}\033[0m")
                return
    write_fail("URL de canal introuvable dans --info")

# ======================================================================
#  DÉPLOIEMENT COMPLET D'UN NŒUD
# ======================================================================

def deploy_node(node_type):
    """Déploie + vérifie un nœud. Renvoie True si conforme (10/10)."""
    node = CONFIG["Nodes"][node_type]
    name = node["Long"]

    try:
        set_common_settings(node["Long"], node["Short"])
        set_role_settings(node_type)
        set_private_channel()
        reset_nodedb()

        pos = "86400" if node_type == "Maison" else "21600"
        ok = test_deployment(pos)
        show_channel_fingerprint()

        print("")
        if ok:
            print(f"  \033[92m✅ Nœud '{name}' déployé ET vérifié.\033[0m")
        else:
            print(f"  \033[91m⚠ Nœud '{name}' déployé mais NON conforme — voir échecs.\033[0m")
        return ok
    except Exception as e:
        print("")
        write_fail(f"Déploiement interrompu : {e}")
        return False

def deploy_standard_node(long_name, short_name, mobile, role, purge_nodedb=False):
    """Nœud hors MeshBridge : conforme Netiquette, sur le canal public
    par défaut (pas de canal privé, pas de PSK).
    Rôle selon la règle Netiquette : CLIENT_MUTE en zone dense ou pour
    tout nœud transporté ; CLIENT en zone peu couverte, pour aider le
    mesh. Rebroadcast ALL (défaut Netiquette — n'a d'effet qu'en CLIENT)."""
    pos_secs = "21600" if mobile else "86400"
    fixed = "false" if mobile else "true"

    try:
        set_common_settings(long_name, short_name, etape="[1/2]")

        write_title(f"[2/2] Règles Netiquette ({'mobile / position 6h' if mobile else 'fixe / position 24h'}, rôle {role})")

        # Même exigence que MeshBridge : sans canal 0 public, le nœud serait
        # sur une autre fréquence que le mesh suisse (invisible, non relayé).
        # Les éventuels canaux perso (index ≥ 1) sont laissés tels quels.
        set_public_primary()

        write_step("Écriture des réglages de position...")
        invoke_meshtastic([
            "--set", "position.position_broadcast_secs", pos_secs,
            "--set", "position.fixed_position", fixed
        ])
        time.sleep(3)

        write_step("Écriture du rôle (peut faire rebooter le nœud)...")
        invoke_meshtastic([
            "--set", "device.role", role,
            "--set", "device.rebroadcast_mode", "ALL"
        ])
        time.sleep(3)

        if purge_nodedb:
            reset_nodedb()

        ok = test_deployment(pos_secs, rebroadcast="ALL", role=role,
                             strict_channels=False)
        print("")
        if ok:
            print(f"  \033[92m✅ Nœud standard '{long_name}' déployé ET vérifié (Netiquette).\033[0m")
        else:
            print(f"  \033[91m⚠ Nœud standard '{long_name}' déployé mais NON conforme — voir échecs.\033[0m")
        return ok
    except Exception as e:
        print("")
        write_fail(f"Déploiement interrompu : {e}")
        return False

# ======================================================================
#  BLE PIERRE
# ======================================================================

def enable_ble_pierre():
    write_title("Activation du BLE sur Pierre")
    pin = os.environ.get("PIERRE_BLE_PIN")
    if not pin or not re.match(r'^\d{6}$', pin.strip()):
        write_fail("PIERRE_BLE_PIN doit être un PIN à 6 chiffres dans .env")
        return

    pin = pin.strip()
    write_step(f"PIN utilisé : {pin} (défini dans .env)")
    try:
        invoke_meshtastic([
            "--set", "bluetooth.enabled", "true",
            "--set", "bluetooth.mode", "FixedPin",
            "--set", "bluetooth.fixed_pin", pin
        ])
        write_ok("BLE activé sur Pierre.")
        print("")
        print("\033[93mProchaines étapes côté Raspberry Pi :\033[0m")
        print("  1) Débrancher Pierre de ce PC, l'alimenter près de la fenêtre.")
        print("  2) Sur le Pi : bluetoothctl → scan on → repérer 'Meshtastic_*'")
        print("  3) pair / trust avec le PIN " + pin)
        print("  4) Reporter la MAC affichée dans PIERRE_BLE_MAC (.env du Pi)")
        print("  5) Installer le bridge : README, étapes 3 et 4 (venv + service)")
    except Exception as e:
        write_fail(f"Échec de l'activation du BLE : {e}")

# ======================================================================
#  ASSISTANT
# ======================================================================

def assist_node():
    """Prend en charge le nœud actuellement branché, de bout en bout."""
    wait_for_single_node()

    write_step("Identification du firmware…")
    est_meshtastic, version, owner = probe_node()

    if not est_meshtastic:
        write_fail("Le nœud n'a pas répondu au protocole Meshtastic.")
        print("\033[93m  Deux causes possibles :\033[0m")
        print("\033[93m  • Le nœud démarrait encore → réessayer dans quelques secondes.\033[0m")
        print("\033[93m  • Un autre firmware tourne (p. ex. MeshCore, livré sur certains\033[0m")
        print("\033[93m    appareils Seeed) → le passer sous Meshtastic via :\033[0m")
        print("\033[93m       https://flasher.meshtastic.org\033[0m")
        print("\033[93m       (nRF52 : double-clic sur reset → glisser le .uf2 sur le disque USB)\033[0m")
        return

    write_ok(f"Firmware Meshtastic {version or '(version illisible)'} détecté")
    derniere = latest_stable_firmware()
    if version and derniere:
        if tuple(map(int, version.split("."))) < tuple(map(int, derniere.split("."))):
            print(f"  \033[93m[i] Version {version} < dernière stable {derniere} — mise à jour conseillée")
            print(f"      via https://flasher.meshtastic.org (la Netiquette recommande de rester à jour)\033[0m")
        else:
            write_ok(f"Firmware à jour (dernière stable : {derniere})")

    # Nœud déjà configuré → confirmer le rôle, vérifier, re-déployer si besoin.
    # Noms tirés de CONFIG : un renommage n'a qu'un seul endroit à modifier.
    known = {node["Long"]: t for t, node in CONFIG["Nodes"].items()}
    node_type = None
    ok = False

    if owner in known:
        role = known[owner]
        write_ok(f"Nœud reconnu : {owner} ({'fixe' if role == 'Maison' else 'portable'})")
        # Filet de sécurité : un nœud a pu recevoir le mauvais rôle
        if ask_yes_no("Conserver ce rôle ?"):
            node_type = role
            pos = "86400" if node_type == "Maison" else "21600"
            ok = test_deployment(pos)
            if not ok and ask_yes_no("\nRe-déployer la configuration complète ?"):
                ok = deploy_node(node_type)
            elif ok:
                show_channel_fingerprint()

    if node_type is None:
        # Nœud vierge, nom inconnu, ou rôle refusé → choisir son rôle
        maison = CONFIG["Nodes"]["Maison"]["Long"]
        portable = CONFIG["Nodes"]["Portable"]["Long"]
        if owner not in known:
            print(f"\n  Nœud non configuré détecté (nom actuel : {owner or 'inconnu'}).")
        print("  Quel rôle lui attribuer ?")
        print(f"    1. {maison}  — fixe, relié au Raspberry Pi (passerelle Internet)")
        print(f"    2. {portable}  — portable, accompagne le téléphone")
        print("    3. Nœud standard  — hors MeshBridge, conforme Netiquette (canal public)")
        while True:
            choix = input("  Choix (1/2/3) : ").strip()
            if choix in ("1", "2", "3"):
                break

        if choix == "3":
            while True:
                long_name = input("  Nom long du nœud (neutre de préférence) : ").strip()
                if long_name:
                    break
            while True:
                short_name = input("  Nom court (2 à 4 caractères) : ").strip()
                if 2 <= len(short_name) <= 4:
                    break
            mobile = ask_yes_no("  Nœud mobile (sac, voiture) plutôt que fixe ?")
            if mobile:
                # Netiquette : tout nœud transporté reste en CLIENT_MUTE
                role = "CLIENT_MUTE"
            else:
                dense = ask_yes_no("  Le mesh est-il déjà dense ici (>50 nœuds visibles dans l'app) ?")
                role = "CLIENT_MUTE" if dense else "CLIENT"
                if role == "CLIENT":
                    print("  \033[90m(zone peu couverte → CLIENT : le nœud aidera à relayer)\033[0m")
            purge_db = ask_yes_no("  Vider aussi la liste des nœuds connus (repart de zéro) ?")
            node_type = "Standard"
            ok = deploy_standard_node(long_name, short_name, mobile, role,
                                      purge_nodedb=purge_db)
        else:
            node_type = "Maison" if choix == "1" else "Portable"
            ok = deploy_node(node_type)

    # Pierre conforme → proposer le BLE dans la foulée (l'étape s'oubliait)
    if node_type == "Maison" and ok:
        if ask_yes_no("\nActiver le BLE sur Pierre maintenant (liaison avec le Pi) ?"):
            enable_ble_pierre()

def main():
    print("\033[96m=================================================\033[0m")
    print("   \033[97mASSISTANT DE CONFIGURATION MESHBRIDGE\033[0m")
    print("   \033[90mNetiquette Suisse — Janvier 2026\033[0m")
    print("\033[96m=================================================\033[0m")

    test_prerequisites()

    while True:
        assist_node()
        if not ask_yes_no("\nConfigurer un autre nœud ?"):
            break
        input("Débrancher le nœud actuel, brancher l'autre, puis Entrée… ")

    print("\nTerminé. Étape suivante : appairage BLE côté Pi (README, étape 2).")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrompu par l'utilisateur.")
        sys.exit(0)
