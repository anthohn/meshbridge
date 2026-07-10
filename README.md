# MeshBridge

Relai Internet **off-grid** : accéder au web depuis un iPhone en **mode avion**, via un réseau radio LoRa privé, sans dépendre d'un opérateur télécom.

Projet de diplôme — École Supérieure d'Informatique de Gestion, Suisse.

---

## Architecture

```
iPhone (mode avion)
   │  Bluetooth
Nimbus  (LilyGO, portable)
   │  LoRa 868 MHz — canal privé chiffré
Aurora  (Heltec V3, fixe contre la fenêtre)
   │  Bluetooth Low Energy
Raspberry Pi  →  Internet
```

Le téléphone n'a jamais de connexion télécom. Toutes les commandes transitent par radio LoRa jusqu'au Raspberry Pi, qui va chercher l'information sur le web et la renvoie **compressée par IA** pour tenir dans un paquet radio (~200 octets).

Aurora est physiquement séparé du Pi (lien BLE) pour être placé à l'endroit radio-optimal (fenêtre, hauteur), sans être contraint par le trajet d'un câble USB.

---

## Structure du repo

```
meshbridge/
├── src/                       # Code du relai (tourne sur le Raspberry Pi)
│   ├── bridge.py              #   point d'entrée : boucle mesh, worker non bloquant
│   ├── config.py              #   constantes + chargement du .env
│   ├── ai.py                  #   compression IA en cascade (cloud → local → brut)
│   ├── commands.py            #   registre des commandes /… + répartiteur
│   ├── metrics.py             #   compteurs /stats + journal metrics.csv
│   └── formatting.py          #   troncature LoRa + étiquette de source
├── scripts/
│   ├── config_meshbridge.py   # Déploie + vérifie la config des 2 nœuds (Python)
│   └── Config-MeshBridge.ps1  # Déploie + vérifie la config (PowerShell, déprécié)
├── tests/                     # Suite pytest (lancée en CI à chaque push)
├── deploy/
│   └── meshbridge.service     # Unit systemd : démarrage au boot + relance auto
├── .env.example
├── requirements.txt
└── .gitignore
```

---

## Prérequis

- 2 nœuds Meshtastic (LilyGO TTGO + Heltec V3, ou équivalent), firmware ≥ 2.7.0
- Raspberry Pi avec Python 3.10+ et Bluetooth actif (Pi 4/5 par défaut)
- [Ollama](https://ollama.com) installé sur le Pi (fallback IA hors-ligne)
- Une clé API Gemini (optionnelle — le système fonctionne sans, en mode local uniquement)
- Un PC (Windows/Linux) avec la CLI `meshtastic` pour la configuration initiale des nœuds (USB)

---

## Configuration des secrets (.env)

Un seul fichier `.env` à la racine, jamais committé, lu automatiquement par les deux scripts.

```bash
cp .env.example .env
```

Compléter ensuite avec les valeurs réelles :

```
MESHBRIDGE_PSK=ta-cle-base64          # côté PC (config des nœuds)
GEMINI_API_KEY=ta-cle-gemini          # côté Pi (bridge.py)
AURORA_BLE_MAC=AA:BB:CC:DD:EE:FF      # côté Pi (adresse BLE d'Aurora)
AURORA_BLE_PIN=123456                 # côté PC (activation BLE d'Aurora)
```

⚠️ **Le `.env` doit être copié sur chaque machine séparément** (PC pour la configuration des nœuds, Raspberry Pi pour le bridge). Ce n'est pas un fichier synchronisé — chaque machine a besoin des variables qui la concernent. Pour générer une clé PSK :

```bash
python3 -c "import os, base64; print(base64.b64encode(os.urandom(32)).decode())"
```

---

## Installation

### 1. Configuration initiale des nœuds (PC, Aurora branché en USB)

```bash
pip install meshtastic python-dotenv
python3 scripts/config_meshbridge.py
```

Options du menu, dans l'ordre :

1. **Déployer + vérifier** Aurora (Heltec, maison) → 7/7 attendu
2. **Déployer + vérifier** Nimbus (LilyGO, portable) → 7/7 attendu
3. **Activer le BLE sur Aurora** (option 4 du menu) — lit `AURORA_BLE_PIN` du `.env`

### 2. Appairage BLE Pi ↔ Aurora (une seule fois)

Débrancher Aurora du PC et l'alimenter à sa position définitive (fenêtre). Puis, sur le Pi :

```bash
bluetoothctl
> scan on
# attendre l'apparition de "Meshtastic_XXXX"
> pair AA:BB:CC:DD:EE:FF   # remplacer par la MAC réelle
# → saisir le PIN défini plus haut
> trust AA:BB:CC:DD:EE:FF
> exit
```

Noter la MAC affichée et la reporter dans le `.env` du Pi (`AURORA_BLE_MAC=...`).

### 3. Installation du bridge (Pi)

Dans un environnement virtuel, pour ne pas toucher au Python du système :

```bash
cd ~/meshbridge
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
ollama pull llama3.2:1b
```

Premier lancement à la main, pour vérifier que tout fonctionne :

```bash
.venv/bin/python3 src/bridge.py
```

Le message `[BLE] connecté. Nœud local : xxxxxx` doit apparaître. Si le lien BLE décroche (interférences WiFi, distance, reboot d'Aurora), le bridge se reconnecte automatiquement avec un délai croissant (5s → 60s max).

> 💡 Sur un Pi, le WiFi 2,4 GHz et le Bluetooth partagent la même antenne. Si le routeur propose du 5 GHz, y connecter le Pi réduit nettement les décrochages BLE.

### 4. Démarrage automatique (service systemd)

Une fois le lancement manuel validé (Ctrl-C pour l'arrêter), installer le bridge comme service système : il démarrera au boot et sera relancé automatiquement en cas de crash.

```bash
sudo cp deploy/meshbridge.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now meshbridge
```

> ⚠️ Le fichier service contient l'utilisateur (`antho`) et le chemin du repo (`/home/antho/meshbridge`) en dur — à adapter si l'installation diffère.

Commandes utiles :

```bash
systemctl status meshbridge      # état du service
journalctl -u meshbridge -f      # logs en direct
sudo systemctl restart meshbridge
```

Vérifier également qu'Ollama est lui-même un service (c'est le cas s'il a été installé via le script officiel) :

```bash
systemctl status ollama          # doit afficher "active (running)"
sudo systemctl enable --now ollama   # sinon, l'activer
```

---

## Commandes disponibles (canal MeshBridge)

Toutes les commandes commencent par `/`. Un message sans `/` est ignoré (le canal reste utilisable pour discuter normalement).

| Commande | Effet |
|---|---|
| `/ping` | Test de connectivité |
| `/meteo <ville>` | Météo compacte (source directe API) |
| `/news` | Titres d'actualité résumés |
| `/web <url>` | Résumé IA de n'importe quelle page |
| `/ask <question>` | Réponse directe d'une IA |
| `/stats` | État du relai : uptime, nb de requêtes, répartition cloud/local, latence moyenne |
| `/help` | Liste des commandes |

Chaque requête traitée est aussi journalisée dans `metrics.csv` (racine du repo, ignoré par Git) : horodatage, commande, mode, source IA, tailles avant/après compression, latence. De quoi mesurer le taux de compression réel et comparer Gemini/Ollama sur la durée.

Les réponses sont préfixées d'un emoji indiquant leur source : `⚡` Gemini (cloud), `🏠` Ollama (local), `✂️` texte brut tronqué (aucune IA disponible).

**Choix de l'IA par requête** — surcharge ponctuelle avec `!local` ou `!cloud` en fin de commande. Pratique pour comparer qualité et vitesse des deux backends :

```
/ask capitale du Japon !local
/web https://fr.wikipedia.org/wiki/LoRa !cloud
```

---

## Tests

La logique du bridge (troncature LoRa, cascade IA, parsing des commandes, garde-fous de `/web`, métriques) est couverte par une suite pytest, sans réseau ni matériel requis :

```bash
pip install pytest
pytest
```

La suite tourne aussi automatiquement sur GitHub Actions à chaque push.

---

## Conformité

Configuration alignée sur la *Netiquette Meshtastic Suisse* (édition janvier 2026) : preset `MEDIUM_FAST`, rôle `CLIENT_MUTE`, hop limit 3, respect du duty cycle légal de 10 % (EU_868).

---

## Roadmap

- [x] **Niveau 1** — Relai intelligent (LoRa + compression IA cloud/local)
- [ ] **Niveau 2** — Protocole custom, chiffrement E2E, app iOS native
- [ ] **Niveau 3** — Routage anonyme, réseau multi-nœuds

---

## Sécurité

Ce repo ne contient **aucun secret**. PSK du canal privé, clé API Gemini, MAC et PIN BLE sont fournis via `.env` (ignoré par Git) au moment de l'exécution. Seul `.env.example` — un modèle vide — est versionné.