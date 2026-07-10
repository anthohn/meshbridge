#!/usr/bin/env python3
"""
MeshBridge — installation du service systemd.
Détecte l'utilisateur courant et l'emplacement du repo, remplit le modèle
meshbridge.service, l'installe et le démarre. Relançable sans risque.

Usage :  python3 deploy/install.py   (sans sudo — il est demandé au bon moment)
"""
import getpass
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
VENV_PYTHON = REPO / ".venv" / "bin" / "python3"
TEMPLATE = REPO / "deploy" / "meshbridge.service"
TARGET = "/etc/systemd/system/meshbridge.service"


def main():
    user = getpass.getuser()
    if user == "root":
        print("Lancer ce script sans sudo : les droits root ne sont demandés")
        print("que pour l'écriture dans /etc (sinon le service serait installé")
        print("au nom de root au lieu de l'utilisateur réel).")
        sys.exit(1)

    if not VENV_PYTHON.exists():
        print(f"Erreur : venv introuvable dans {REPO}/.venv")
        print("Créer d'abord l'environnement (README, étape 3) :")
        print("  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt")
        sys.exit(1)

    print(f"Installation du service (utilisateur : {user}, repo : {REPO})")
    unit = (TEMPLATE.read_text(encoding="utf-8")
            .replace("__USER__", user)
            .replace("__REPO__", str(REPO)))

    # L'écriture dans /etc demande les droits root → sudo tee
    subprocess.run(["sudo", "tee", TARGET], input=unit, text=True,
                   check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["sudo", "systemctl", "daemon-reload"], check=True)
    subprocess.run(["sudo", "systemctl", "enable", "--now", "meshbridge"], check=True)

    print()
    subprocess.run(["systemctl", "status", "meshbridge", "--no-pager", "--lines=5"])


if __name__ == "__main__":
    main()
