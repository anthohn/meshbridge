#!/usr/bin/env python3
"""
MeshBridge — Déploiement et vérification de nœuds Meshtastic
Normes Netiquette Suisse (janvier 2026) + canal privé chiffré.

Configure un nœud Meshtastic de façon reproductible et VÉRIFIÉE :
chaque paramètre critique est relu après écriture et comparé à la
valeur attendue. Le script ne déclare le succès que si toutes les
vérifications passent.
"""

import os
import sys
import subprocess
import re
import shutil
import time
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
    print("\033[93m    Copie .env.example en .env et remplis tes clés.\033[0m")

# ======================================================================
#  CONFIGURATION GLOBALE
# ======================================================================
CONFIG = {
    "PSK": os.environ.get("MESHBRIDGE_PSK"),
    "Channel": 1,         # index du canal privé MeshBridge
    "MinFirmware": "2.7.0",   # version minimale recommandée

    # Noms NEUTRES diffusés dans le NodeInfo public (toutes les 3h, en clair).
    "Nodes": {
        "Maison": {"Long": "Aurora", "Short": "AUR"},
        "Portable": {"Long": "Nimbus", "Short": "NIM"}
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
                if attempt < max_retries:
                    print(f"  \033[90m[!] Nœud hors-ligne (reboot ?). Nouvel essai dans {retry_delay}s... ({attempt}/{max_retries})\033[0m")
                    time.sleep(retry_delay)
                    continue
            raise RuntimeError(f"meshtastic {' '.join(args)} a échoué (code {e.returncode}) :\n{output}")
        except FileNotFoundError:
            raise RuntimeError("La CLI 'meshtastic' est introuvable. Installez-la avec : pip install meshtastic")
    
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
#  PRÉREQUIS
# ======================================================================

def test_prerequisites():
    write_title("Vérification des prérequis...")

    if not CONFIG["PSK"] or not CONFIG["PSK"].strip():
        write_fail("MESHBRIDGE_PSK manquant ou vide.")
        print("\033[93m  Vérifie que .env existe à la racine du repo et contient MESHBRIDGE_PSK=...\033[0m")
        sys.exit(1)

    if not shutil.which("meshtastic"):
        write_fail("CLI 'meshtastic' introuvable. Installe-la : pip install meshtastic")
        sys.exit(1)

    try:
        raw_version = invoke_meshtastic(["--version"])
        version = raw_version.splitlines()[0].strip()
        write_ok(f"Logiciel détecté — firmware/CLI {version}")
    except Exception as e:
        write_fail(f"Aucun nœud accessible. Branche-le en USB.\n{e}")
        sys.exit(1)

# ======================================================================
#  APPLICATION DE LA CONFIGURATION
# ======================================================================

def set_common_settings(long_name, short_name):
    write_title(f"[1/3] Identité et paramètres communs ({long_name} / {short_name})")

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

    invoke_meshtastic([
        "--set", "device.role", "CLIENT_MUTE",
        "--set", "device.rebroadcast_mode", "LOCAL_ONLY",
        "--set", "position.position_broadcast_secs", pos_secs,
        "--set", "position.fixed_position", fixed
    ])
    time.sleep(3)

def set_private_channel():
    write_title(f"[3/3] Canal privé chiffré MeshBridge (index {CONFIG['Channel']})")
    invoke_meshtastic([
        "--ch-index", str(CONFIG["Channel"]),
        "--ch-set", "name", "MeshBridge",
        "--ch-set", "psk", f"base64:{CONFIG['PSK']}",
        "--ch-set", "uplink_enabled", "false",
        "--ch-set", "downlink_enabled", "false"
    ])
    time.sleep(5) # Pause plus longue après la configuration finale des canaux avant vérification

# ======================================================================
#  VÉRIFICATION POST-DÉPLOIEMENT
# ======================================================================

def test_deployment(node_type):
    write_title("Vérification post-déploiement (relecture réelle des champs)")

    pos_expected = "86400" if node_type == "Maison" else "21600"

    results = [
        test_setting("lora.region", "EU_868", "Région"),
        test_setting("lora.modem_preset", "MEDIUM_FAST", "Preset LoRa"),
        test_setting("lora.hop_limit", "3", "Hop limit"),
        test_setting("device.role", "CLIENT_MUTE", "Rôle"),
        test_setting("device.rebroadcast_mode", "LOCAL_ONLY", "Rebroadcast"),
        test_setting("position.position_broadcast_smart_enabled", "False", "Smart position"),
        test_setting("position.position_broadcast_secs", pos_expected, "Position interval")
    ]

    passed = sum(1 for r in results if r)
    total = len(results)

    print("")
    if passed == total:
        print(f"  \033[92m✅ {passed}/{total} vérifications réussies — nœud {node_type} conforme.\033[0m")
        return True
    else:
        print(f"  \033[91m❌ {passed}/{total} réussies — {total - passed} à corriger ci-dessus.\033[0m")
        return False

def show_channel_url():
    write_title("URL des canaux (doit être IDENTIQUE sur les deux nœuds)")
    print("\033[90m  Compare cette ligne entre Maison et Portable : PSK partagé = OK\033[0m")
    info = invoke_meshtastic(["--info"])
    for line in info.splitlines():
        if "Complete URL" in line:
            print(line)

# ======================================================================
#  ORCHESTRATION
# ======================================================================

def deploy_node(node_type):
    node = CONFIG["Nodes"][node_type]
    name = node["Long"]

    try:
        set_common_settings(node["Long"], node["Short"])
        set_role_settings(node_type)
        set_private_channel()

        ok = test_deployment(node_type)
        show_channel_url()

        print("")
        if ok:
            print("\033[92m════════════════════════════════════════════════════\033[0m")
            print(f"  \033[92m✅ Nœud '{name}' déployé ET vérifié.\033[0m")
            print("\033[92m════════════════════════════════════════════════════\033[0m")
        else:
            print("\033[91m════════════════════════════════════════════════════\033[0m")
            print(f"  \033[91m⚠ Nœud '{name}' déployé mais NON conforme — voir échecs.\033[0m")
            print("\033[91m════════════════════════════════════════════════════\033[0m")
    except Exception as e:
        print("")
        write_fail(f"Déploiement interrompu : {e}")

# ======================================================================
#  BLE AURORA
# ======================================================================

def enable_ble_aurora():
    write_title("Activation du BLE sur Aurora")
    pin = os.environ.get("AURORA_BLE_PIN")
    if not pin or not re.match(r'^\d{6}$', pin.strip()):
        write_fail("AURORA_BLE_PIN doit être un PIN à 6 chiffres dans .env")
        return
    
    pin = pin.strip()
    write_step(f"PIN utilisé : {pin} (défini dans .env)")
    try:
        invoke_meshtastic([
            "--set", "bluetooth.enabled", "true",
            "--set", "bluetooth.mode", "FixedPin",
            "--set", "bluetooth.fixed_pin", pin
        ])
        write_ok("BLE activé sur Aurora.")
        print("")
        print("\033[93mProchaines étapes côté Raspberry Pi :\033[0m")
        print("  1) Débranche Aurora de ce PC, alimente-le près de la fenêtre.")
        print("  2) Sur le Pi : bluetoothctl → scan on → repère 'Meshtastic_*'")
        print("  3) pair / trust / connect avec le PIN " + pin)
        print("  4) Note la MAC affichée, mets-la dans AURORA_BLE_MAC (.env du Pi)")
        print("  5) python3 src/bridge.py")
    except Exception as e:
        write_fail(f"Échec de l'activation du BLE : {e}")

# ======================================================================
#  MENU INTERACTIF
# ======================================================================

def show_menu():
    os.system('cls' if os.name == 'nt' else 'clear')
    print("\033[96m=================================================\033[0m")
    print("   \033[97mGESTIONNAIRE DE DÉPLOIEMENT MESHBRIDGE\033[0m")
    print("   \033[90mNetiquette Suisse - Janvier 2026\033[0m")
    print("\033[96m=================================================\033[0m")
    print(" 1. Déployer + vérifier le Pont Maison (Heltec v3)")
    print(" 2. Déployer + vérifier le Nœud Portable (LilyGO)")
    print(" 3. Vérifier seulement le nœud branché")
    print(" 4. Activer le BLE sur Aurora (pour le lier au Pi sans USB)")
    print(" 5. Quitter")
    print("-------------------------------------------------")

def main():
    test_prerequisites()
    input("\nPrérequis OK. Appuie sur Entrée pour continuer...")

    while True:
        show_menu()
        choix = input("Sélectionne une option (1-5) : ").strip()

        if choix == '1':
            deploy_node("Maison")
            input("\nEntrée pour revenir au menu")
        elif choix == '2':
            deploy_node("Portable")
            input("\nEntrée pour revenir au menu")
        elif choix == '3':
            try:
                # Type déduit de l'intervalle de position lu sur le nœud
                pos = get_setting("position.position_broadcast_secs")
                node_type = "Maison" if pos == "86400" else "Portable"
                print(f"  \033[90m(nœud détecté comme : {node_type}, position={pos} s)\033[0m")
                test_deployment(node_type)
                show_channel_url()
            except Exception as e:
                write_fail(str(e))
            input("\nEntrée pour revenir au menu")
        elif choix == '4':
            enable_ble_aurora()
            input("\nEntrée pour revenir au menu")
        elif choix == '5':
            print("Fermeture...")
            break
        else:
            print("\033[91mOption invalide.\033[0m")
            time.sleep(1)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrompu par l'utilisateur.")
        sys.exit(0)
