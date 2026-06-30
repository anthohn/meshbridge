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
Aurora  (Heltec V3, fixe à la maison)
   │  USB
Raspberry Pi  →  Internet
```

Le téléphone n'a jamais de connexion télécom. Toutes les commandes transitent par radio LoRa jusqu'au Raspberry Pi, qui va chercher l'information sur le web et la renvoie **compressée par IA** pour tenir dans un paquet radio (~200 octets).

Détails complets : [`docs/MeshBridge-Notes.md`](docs/MeshBridge-Notes.md)

---

## Structure du repo

```
meshbridge/
├── src/
│   └── bridge.py              # Tourne sur le Raspberry Pi
├── scripts/
│   └── Config-MeshBridge.ps1  # Déploie + vérifie la config des 2 nœuds
├── docs/
│   └── MeshBridge-Notes.md    # Notes de fonctionnement détaillées
├── requirements.txt
└── .gitignore
```

---

## Prérequis

- 2 nœuds Meshtastic (LilyGO TTGO + Heltec V3, ou équivalent), firmware ≥ 2.7.0
- Raspberry Pi avec Python 3.10+, connecté en USB au nœud fixe
- [Ollama](https://ollama.com) installé sur le Pi (fallback IA hors-ligne)
- Une clé API Gemini (optionnelle — le système fonctionne sans, en mode local uniquement)

---

## Configuration des secrets (.env)

Un seul fichier `.env` à la racine, jamais committé, lu automatiquement par les deux scripts.

```bash
cp .env.example .env
```

Remplis-le avec tes vraies clés :

```
MESHBRIDGE_PSK=ta-cle-base64
GEMINI_API_KEY=ta-cle-gemini
```

⚠️ **Copie ce `.env` sur chaque machine séparément** (PC Windows pour la config des nœuds, Raspberry Pi pour le bridge) — ce n'est pas un fichier synchronisé, juste un modèle commun. Génère une clé PSK si tu n'en as pas :

```powershell
[Convert]::ToBase64String((1..32 | ForEach-Object { Get-Random -Max 256 }))
```

---

## Installation

### 1. Côté Raspberry Pi

```bash
pip install -r requirements.txt --break-system-packages
ollama pull llama3.2:1b

python3 src/bridge.py   # charge .env automatiquement
```

### 2. Configuration des nœuds (depuis un PC avec la CLI `meshtastic`)

```powershell
pip install meshtastic

.\scripts\Config-MeshBridge.ps1   # charge .env automatiquement
```

---

## Commandes disponibles (canal MeshBridge)

| Commande | Effet |
|---|---|
| `PING` | Test de connectivité |
| `METEO <ville>` | Météo compacte |
| `NEWS` | Titres d'actualité résumés |
| `WEB <url>` | Résumé IA de n'importe quelle page |
| `ASK <question>` | Réponse directe d'une IA |
| `HELP` | Liste des commandes |

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

Ce repo ne contient **aucun secret**. Le PSK du canal privé et la clé API Gemini sont fournis via variables d'environnement au moment de l'exécution, jamais committés.
