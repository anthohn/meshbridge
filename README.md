# MeshBridge

Relai Internet **off-grid** : accéder au web depuis un iPhone en **mode avion**, via un réseau radio LoRa privé, sans dépendre d'un opérateur télécom.

Projet de diplôme — École Supérieure d'Informatique de Gestion, Suisse.

---

## Architecture

```
iPhone (mode avion)
   │  Bluetooth
Paul  (LilyGO, portable)
   │  LoRa 868 MHz — canal privé chiffré
Pierre  (Heltec V3, fixe contre la fenêtre)
   │  Bluetooth Low Energy
Raspberry Pi  →  Internet
```

Le téléphone n'a jamais de connexion télécom. Toutes les commandes transitent par radio LoRa jusqu'au Raspberry Pi, qui va chercher l'information sur le web et la renvoie **compressée par IA** pour tenir dans un paquet radio (~200 octets).

Les nœuds vivent sur **deux canaux** : le canal 0 est le canal public suisse — il fixe la fréquence radio et permet aux autres nœuds du mesh de **relayer** le trafic (portée étendue) — tandis que les commandes passent sur le canal 1 `MeshBridge`, chiffré : les relais publics transportent ces paquets sans pouvoir les lire.

Pierre est physiquement séparé du Pi (lien BLE) pour être placé à l'endroit radio-optimal (fenêtre, hauteur), sans être contraint par le trajet d'un câble USB.

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
│   ├── config_meshbridge.py   # Assistant de config des nœuds (détection USB, pas à pas)
│   └── Config-MeshBridge.ps1  # Déploie + vérifie la config (PowerShell, déprécié)
├── tests/                     # Suite pytest (lancée en CI à chaque push)
├── docs/
│   └── netiquette-meshtastic-suisse-janvier-2026.pdf   # Référence de conformité
├── deploy/
│   ├── install.py             # Installe le service systemd (chemins auto-détectés)
│   └── meshbridge.service     # Modèle d'unit : démarrage au boot + relance auto
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

Cloner d'abord le projet **sur chaque machine** — le PC (configuration des nœuds) et le Raspberry Pi (bridge) :

```bash
git clone https://github.com/anthohn/meshbridge.git
cd meshbridge
```

Puis créer le fichier de secrets à la racine (jamais committé, lu automatiquement par le script de configuration et par le bridge) :

```bash
cp .env.example .env
```

Compléter ensuite avec les valeurs réelles :

```
MESHBRIDGE_PSK=cle-base64             # côté PC — à générer, voir ci-dessous
GEMINI_API_KEY=cle-gemini             # côté Pi — optionnelle (sinon IA locale seule)
PIERRE_BLE_MAC=AA:BB:CC:DD:EE:FF      # côté Pi — obtenue à l'étape 2 de l'installation
PIERRE_BLE_PIN=123456                 # côté PC — PIN à 6 chiffres, choisi librement
```

⚠️ **Le `.env` n'est pas synchronisé entre les machines** : en créer un sur chaque machine, avec les variables qui la concernent. Pour générer la clé PSK :

```bash
python3 -c "import os, base64; print(base64.b64encode(os.urandom(32)).decode())"
```

---

## Installation

### 1. Configuration initiale des nœuds (PC, un nœud à la fois en USB)

```bash
pip install meshtastic python-dotenv
python3 scripts/config_meshbridge.py
```

Le script est un **assistant** : il détecte le nœud branché en USB et guide pas à pas — il refuse de continuer si deux nœuds sont branchés en même temps, demande le rôle à attribuer au nœud (Pierre fixe / Paul portable / nœud standard), **repart d'une config d'usine** (garantit un état connu quelle que soit l'histoire du nœud — l'appairage BLE avec le Pi est préservé), déploie puis vérifie la configuration en relisant **chaque champ écrit** (23/23 attendu — identité, réglages LoRa, désactivation des modules bavards `neighbor_info`/`range_test`/`store_forward`, **et** table des canaux : canal 0 public, canal 1 MeshBridge, aucun canal résiduel ; les champs en écart sont réécrits de façon ciblée puis revérifiés), vide la liste des nœuds connus (NodeDB — elle se repeuple à l'écoute du mesh), et propose d'activer le BLE juste après Pierre. Suivre les instructions à l'écran pour traiter les deux nœuds l'un après l'autre.

L'assistant propose aussi un mode **nœud standard** (option 3) : configuration conforme à la Netiquette pour un nœud qui ne fait pas partie de MeshBridge — canal public uniquement, sans PSK. Le rôle suit la règle de la Netiquette : `CLIENT_MUTE` en zone dense ou pour tout nœud transporté, `CLIENT` en zone peu couverte (le nœud aide alors à relayer le mesh).

#### Dépannage (Linux)

- **`Permission denied` sur `/dev/ttyUSB0`/`/dev/ttyACM0`** : l'utilisateur doit appartenir au groupe `dialout` (`sudo usermod -a -G dialout $USER`, puis se déconnecter/reconnecter). L'assistant le détecte et affiche la commande exacte.
- **Timeouts en boucle alors que le nœud est branché** : ModemManager sonde les ports série (il les prend pour des modems). Le désactiver s'il n'est pas utilisé (`sudo systemctl disable --now ModemManager`), ou l'exclure par règle udev (`ENV{ID_MM_DEVICE_IGNORE}="1"` pour le vendor id du nœud), puis débrancher/rebrancher.

### 2. Appairage BLE Pi ↔ Pierre (une seule fois)

Débrancher Pierre du PC et l'alimenter à sa position définitive (fenêtre). Puis, sur le Pi :

```bash
bluetoothctl
> scan on
# attendre une ligne "Meshtastic_XXXX" : elle affiche l'adresse MAC de Pierre
> pair AA:BB:CC:DD:EE:FF   # remplacer par cette MAC
# → saisir le PIN (PIERRE_BLE_PIN du .env, activé par l'assistant à l'étape 1)
> trust AA:BB:CC:DD:EE:FF
> exit
```

Reporter cette MAC dans le `.env` du Pi (`PIERRE_BLE_MAC=...`).

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

Le message `[BLE] connecté. Nœud local : xxxxxx` doit apparaître. Si le lien BLE décroche (interférences WiFi, distance, reboot de Pierre), le bridge se reconnecte automatiquement avec un délai croissant (5s → 60s max).

> 💡 Sur un Pi, le WiFi 2,4 GHz et le Bluetooth partagent la même antenne. Si le routeur propose du 5 GHz, y connecter le Pi réduit nettement les décrochages BLE.

### 4. Démarrage automatique (service systemd)

Une fois le lancement manuel validé (Ctrl-C pour l'arrêter), installer le bridge comme service système : il démarrera au boot et sera relancé automatiquement en cas de crash.

```bash
python3 deploy/install.py
```

Le script détecte l'utilisateur courant et l'emplacement du repo, génère le fichier service à partir du modèle `deploy/meshbridge.service`, l'installe et le démarre (sudo est demandé au moment de l'écriture dans `/etc`). Il peut être relancé sans risque (mise à jour du service après un déplacement du repo, par exemple).

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

### 5. Côté iPhone

Installer l'app [Meshtastic](https://meshtastic.org/docs/software/apple/) (App Store), activer le Bluetooth et se connecter à **Paul** depuis l'app. Le canal `MeshBridge` (configuré à l'étape 1) apparaît dans la liste des canaux : ouvrir sa conversation et envoyer `/ping` — le relai doit répondre `pong ✅ relai actif`. Le mode avion peut ensuite être activé : seul le Bluetooth vers Paul est nécessaire.

---

## Commandes disponibles (canal MeshBridge)

Toutes les commandes commencent par `/`. Un message sans `/` est ignoré (le canal reste utilisable pour discuter normalement).

| Commande | Effet |
|---|---|
| `/ping` | Test de connectivité |
| `/meteo <ville>` | Météo compacte (source directe API) |
| `/news` | Titres d'actualité résumés |
| `/web <url>` | Résumé IA d'une page web publique |
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
pip install pytest        # sur un Linux récent : pip install --user pytest
pytest
```

La suite tourne aussi automatiquement sur GitHub Actions à chaque push.

---

## Conformité

Configuration alignée sur la [*Netiquette Meshtastic Suisse*](docs/netiquette-meshtastic-suisse-janvier-2026.pdf) (version janvier 2026), document communautaire trilingue (DE/FR/EN) inclus dans `docs/` pour référence : preset `MEDIUM_FAST` (standard suisse depuis fin 2025), rôle `CLIENT_MUTE` (recommandé en zone dense), hop limit 3 (« idéal 3 ou 4 »), intervalles de diffusion préconisés (NodeInfo 3 h, position 6 h mobile / 24 h fixe, Smart Position désactivé, télémétrie réduite à 72 h), MQTT ignoré et respect du duty cycle légal de 10 % (EU_868).

Document rédigé par la communauté Meshtastic Suisse (MeshTrafficObserver, Haflinger 73, CamFlyerCH, Fox 71) — inclus ici avec attribution, à titre de référence de configuration.

---

## Sécurité

Ce repo ne contient **aucun secret**. PSK du canal privé, clé API Gemini, MAC et PIN BLE sont fournis via `.env` (ignoré par Git) au moment de l'exécution. Seul `.env.example` — un modèle vide — est versionné.