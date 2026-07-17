#!/usr/bin/env python3
"""
MeshBridge — Assistant de configuration des nœuds Meshtastic
Normes Netiquette Suisse (janvier 2026) + canal privé chiffré.

Principe : brancher UN nœud en USB, lancer le script, se laisser guider.
Chaque déploiement remet le nœud dans un état connu — table des canaux
nettoyée, tous les champs réécrits — puis RELIT le tout champ par champ : le
succès n'est déclaré que si tout concorde. On n'utilise PAS --factory-reset :
il gèle certaines cartes nRF52 (Wio Tracker) en boucle de boot. Le même état
propre est obtenu par des écritures que toute carte supporte (cf. write_profile).
Pour effacer une carte vraiment sale, voir le README (flasher, Erase Flash).

Le fichier est organisé en couches :
  1. ÉTAT DÉSIRÉ      — les profils de nœuds, sous forme de données ;
  2. CONSOLE          — l'affichage et les questions à l'utilisateur ;
  3. LIAISON SÉRIE    — la classe MeshtasticCLI (port épinglé, reconnexions) ;
  4. DÉPLOIEMENT      — écriture ET vérification parcourent le MÊME état
                        désiré : impossible de vérifier autre chose que ce
                        qui a été écrit ;
  5. ASSISTANT        — le déroulé interactif.

Robustesse (le cœur du script) : un nœud reboote souvent (changement de
région, de preset…) et peut réapparaître sur un autre port série. Le port
est donc épinglé, et après chaque écriture on attend que le nœud RÉPONDE de
nouveau (wait_until_ready) — pas seulement que le port réapparaisse. C'est ce
qui évite les « multiple serial ports » ET les écritures perdues (région/preset).
"""

import os
import sys
import re
import time
import shutil
import getpass
import hashlib
import subprocess
from dataclasses import dataclass, replace
from dotenv import load_dotenv

# ======================================================================
#  1. ÉTAT DÉSIRÉ — la source de vérité unique
#     (l'écriture ET la vérification parcourent ces listes)
# ======================================================================

# Réglages communs à tous les nœuds : LoRa sobre, télémétrie espacée, et
# modules bavards OFF (ils émettent en fond et grignotent l'airtime —
# anti-Netiquette). La section lora (région, preset…) allume la radio et
# fait rebooter le nœud : write_profile l'écrit en dernier.
COMMON_SETTINGS = [
    ("lora.region", "EU_868"),
    ("lora.use_preset", "true"),
    ("lora.modem_preset", "MEDIUM_FAST"),
    ("lora.hop_limit", "3"),
    ("lora.override_duty_cycle", "false"),
    ("lora.ignore_mqtt", "true"),
    ("device.node_info_broadcast_secs", "10800"),
    ("position.position_broadcast_smart_enabled", "false"),
    ("telemetry.device_update_interval", "259200"),
    ("telemetry.environment_measurement_enabled", "false"),
    ("telemetry.power_measurement_enabled", "false"),
    ("mqtt.enabled", "false"),
    ("neighbor_info.enabled", "false"),
    ("range_test.enabled", "false"),
    ("store_forward.enabled", "false"),
    ("display.screen_on_secs", "15"),
    ("device.led_heartbeat_disabled", "true"),
    ("position.gps_enabled", "false"),
]

MESHBRIDGE_INDEX = 1   # index du canal privé MeshBridge (le 0 reste public)


@dataclass(frozen=True)
class NodeProfile:
    """Tout ce qui distingue un nœud : son identité et ses réglages propres."""
    long_name: str     # nom NEUTRE, diffusé en clair dans le NodeInfo
    short_name: str
    role: str          # CLIENT (relaie) ou CLIENT_MUTE (silencieux)
    rebroadcast: str   # ALL (défaut Netiquette) ou LOCAL_ONLY
    pos_secs: str      # intervalle d'envoi de la position (secondes)
    fixed: str         # "true" pour un nœud qui ne bouge pas
    meshbridge: bool   # canal privé 1 + vérification stricte des canaux
    modem_preset: str = "MEDIUM_FAST"

    def role_settings(self):
        """Les réglages propres à ce nœud (le reste est commun à tous)."""
        return [
            ("device.role", self.role),
            ("device.rebroadcast_mode", self.rebroadcast),
            ("position.position_broadcast_secs", self.pos_secs),
            ("position.fixed_position", self.fixed),
        ]

    def settings(self):
        """L'état désiré COMPLET du nœud — la vérification relit exactement
        cette liste, champ par champ."""
        custom_common = []
        for field, value in COMMON_SETTINGS:
            if field == "lora.modem_preset":
                custom_common.append((field, self.modem_preset))
            elif field == "lora.hop_limit" and self.modem_preset == "LONG_FAST":
                custom_common.append((field, "5"))
            else:
                custom_common.append((field, value))
        return custom_common + self.role_settings()


# Les deux nœuds du projet : Pierre fixe (relié au Pi), Paul portable.
MESHBRIDGE_NODES = {
    "Maison":   NodeProfile("Pierre", "PIE", "CLIENT", "ALL",
                            pos_secs="86400", fixed="true", meshbridge=True),
    "Portable": NodeProfile("Paul", "PAU", "CLIENT_MUTE", "LOCAL_ONLY",
                            pos_secs="21600", fixed="false", meshbridge=True),
}


def standard_profile(long_name, short_name, mobile, role):
    """Nœud hors MeshBridge : Netiquette seule, canal public, pas de PSK.
    Rebroadcast ALL (défaut Netiquette — n'a d'effet qu'en rôle CLIENT)."""
    return NodeProfile(long_name, short_name, role, "ALL",
                       pos_secs="21600" if mobile else "86400",
                       fixed="false" if mobile else "true",
                       meshbridge=False)


# Traduction code → libellé pour la relecture (--get renvoie parfois le code).
ENUMS = {
    "lora.region": {"3": "EU_868"},
    "lora.modem_preset": {"0": "LONG_FAST", "4": "MEDIUM_FAST"},
    "device.role": {"0": "CLIENT", "1": "CLIENT_MUTE"},
    "device.rebroadcast_mode": {"0": "ALL", "2": "LOCAL_ONLY"},
}

# ======================================================================
#  2. CONSOLE
# ======================================================================
def step(m):  print(f"  \033[90m-> {m}\033[0m")
def ok(m):    print(f"  \033[92m[OK]\033[0m    {m}")
def fail(m):  print(f"  \033[91m[ÉCHEC]\033[0m {m}")
def title(m): print(f"\n\033[96m{m}\033[0m")

def ask_yes_no(question, default=True):
    rep = input(f"{question} [{'O/n' if default else 'o/N'}] : ").strip().lower()
    return default if not rep else rep in ("o", "oui", "y", "yes")

# ======================================================================
#  3. LIAISON SÉRIE — toute la communication avec le nœud passe par ici
# ======================================================================
def meshtastic_ports():
    """Ports des nœuds Meshtastic branchés (détection officielle de la lib)."""
    try:
        import meshtastic.util
        return meshtastic.util.findPorts(True)
    except ImportError:
        fail("Le paquet Python 'meshtastic' est requis : pip install meshtastic")
        sys.exit(1)


class MeshtasticCLI:
    """Enveloppe de la CLI meshtastic. Épingle le port série, s'y reconnecte
    quand le nœud reboote, et n'écrit jamais avant que le nœud réponde :
    toute la robustesse de la liaison est regroupée ici."""

    REBOOT_SIGNS = ("no serial meshtastic device", "connection refused",
                    "error connecting", "no meshtastic device found",
                    "serialexception", "device not found", "timed out",
                    "multiple serial ports", "not found", "file not found",
                    "no such file or directory", "input/output error",
                    "write failed", "read failed")

    def __init__(self):
        self.port = None   # épinglé au premier nœud vu ; ré-épinglé après reboot

    def _command(self, args):
        return ["meshtastic"] + (["--port", self.port] if self.port else []) + list(args)

    def try_run(self, args, timeout):
        """UNE tentative, sans reconnexion — pour sonder un nœud qui ne répond
        peut-être pas. Renvoie la sortie captée (même en cas d'échec ou de
        timeout — ex. port déjà ouvert par une autre appli, qui ne fait
        planter la CLI qu'après avoir imprimé un avertissement), ou None si
        rien n'a pu être capté du tout."""
        def _text(x):
            if x is None:
                return ""
            return x if isinstance(x, str) else x.decode(errors="replace")
        try:
            r = subprocess.run(self._command(args), capture_output=True,
                               text=True, timeout=timeout)
            out = r.stdout + r.stderr
        except subprocess.TimeoutExpired as e:
            # .stdout/.stderr peuvent rester en bytes même avec text=True :
            # la partie captée avant le timeout n'est pas toujours décodée.
            out = _text(e.stdout) + _text(e.stderr)
        except Exception:
            return None
        return out or None

    def node_responds(self):
        """Test de vie RÉEL : le nœud répond-il au protocole ? (port présent ne
        suffit pas — après un reboot, le port réapparaît avant que le nœud
        accepte de la config)."""
        out = self.try_run(["--info"], timeout=15)
        return out is not None and "Owner" in out

    def wait_until_ready(self, timeout=90):
        """Attend que le nœud soit VRAIMENT prêt (un seul port ET --info répond),
        en ré-épinglant le port s'il a changé. C'est LE correctif clé : écrire
        avant que le nœud réponde = écritures perdues (région/preset qui « sautent »)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            ports = meshtastic_ports()
            if len(ports) == 1:
                self.port = ports[0]
                if self.node_responds():
                    return True
            time.sleep(2)
        return False

    def run(self, args, timeout=90):
        """Exécute la CLI, avec reconnexion automatique tant que le nœud
        reboote OU cesse simplement de répondre en cours de lecture (timeout).
        Lève RuntimeError sur un échec non lié à la liaison.
        timeout : borne chaque tentative — sans ça, un nœud qui se tait en
        plein --get/--info bloque le script indéfiniment (rien ne l'interrompt)."""
        for attempt in range(1, 16):
            try:
                r = subprocess.run(self._command(args), capture_output=True,
                                   text=True, check=True, timeout=timeout)
                return r.stdout + r.stderr
            except subprocess.TimeoutExpired:
                print(f"  \033[90m[!] Nœud injoignable (délai dépassé), nouvel essai… ({attempt}/15)\033[0m")
                self.wait_until_ready()      # ré-épingle le port s'il a changé
            except subprocess.CalledProcessError as e:
                out = (e.stdout or "") + (e.stderr or "") or str(e)
                if not any(s in out.lower() for s in self.REBOOT_SIGNS):
                    raise RuntimeError(f"meshtastic {' '.join(args)} a échoué :\n{out}")
                if attempt == 1:
                    cause = next((l.strip() for l in out.splitlines()
                                  if any(s in l.lower() for s in self.REBOOT_SIGNS)), "")
                    print(f"  \033[90m(cause : {cause[:88]})\033[0m")
                print(f"  \033[90m[!] Nœud injoignable (reboot ?), nouvel essai… ({attempt}/15)\033[0m")
                self.wait_until_ready()      # ré-épingle le port s'il a changé
            except FileNotFoundError:
                raise RuntimeError("CLI 'meshtastic' introuvable : pip install meshtastic")
        raise RuntimeError(f"Nœud injoignable après 15 essais : meshtastic {' '.join(args)}")

    def apply(self, args, label, ready_timeout=90, cli_timeout=45):
        """Écriture de config. Le nœud reboote souvent → la CLI peut « échouer »
        alors que l'écriture a réussi (liaison coupée avant l'acquittement) : on
        tolère, puis on ATTEND que le nœud réponde de nouveau avant de continuer.
        Un vrai problème est rattrapé par la vérification finale (relecture).
        cli_timeout : temps laissé à la CLI elle-même — un gros lot sur une
        liaison série lente (Heltec/CP2102) dépasse largement les 45 s."""
        step(label)
        try:
            subprocess.run(self._command(args), capture_output=True, text=True,
                           timeout=cli_timeout)
        except Exception:
            pass
        if not self.wait_until_ready(ready_timeout):
            # Certaines cartes nRF52 (ex. Wio Tracker) restent allumées mais
            # muettes après une écriture : seul un redémarrage au bouton les
            # ranime. On donne sa chance à l'utilisateur avant d'abandonner.
            print("\033[93m  Le nœud ne répond plus (certaines cartes gèlent sur une écriture).\033[0m")
            input("\033[93m  Le redémarrer avec son bouton (appui long), puis Entrée… \033[0m")
            if not self.wait_until_ready(ready_timeout):
                raise RuntimeError("nœud injoignable après écriture (reboot trop long ? câble data ?)")

# ======================================================================
#  4. DÉPLOIEMENT & VÉRIFICATION — les deux faces du même état désiré
# ======================================================================
def set_args(settings):
    """[('a', '1'), ('b', '2')] → ['--set', 'a', '1', '--set', 'b', '2']"""
    args = []
    for field, value in settings:
        args += ["--set", field, value]
    return args


def write_profile(cli, profile):
    """Écrit l'état désiré du profil. Deux règles, tirées de l'observation :
    - une SECTION de config par invocation : la CLI n'acquitte pas les
      écritures d'un nœud USB local, et un lot groupé de 8 sections en perd
      presque toujours une ou deux (device, telemetry) ;
    - la section lora en dernier : tant que la région est vide, la radio est
      éteinte et le nœud est au calme pour recevoir tout le reste.
    L'identité (--set-owner) est un message d'administration, pas une
    écriture de config : elle a son invocation propre."""
    cli.apply(["--set-owner", profile.long_name, "--set-owner-short", profile.short_name],
              f"Identité {profile.long_name} / {profile.short_name}…")
    sections = {}
    for field, value in profile.settings():
        sections.setdefault(field.split(".")[0], []).append((field, value))
    lora = sections.pop("lora")
    for name, settings in sections.items():
        cli.apply(set_args(settings), f"Réglages {name}…")
    cli.apply(set_args(lora), "Réglages lora — région EU_868, la radio s'allume…")
    reset_channels(cli, profile)


def reset_channels(cli, profile):
    """Remet la table des canaux à neuf — le nettoyage que le factory-reset
    nous apportait, mais avec des opérations que TOUTE carte supporte (le
    factory-reset, lui, gèle les nRF52). On supprime les canaux résiduels
    d'une vie antérieure, puis on (re)pose le canal 0 public et, pour un nœud
    MeshBridge, le canal 1 chiffré."""
    keep = 2 if profile.meshbridge else 1   # on garde les index < keep
    info = cli.run(["--info"])
    residual = sorted({int(m.group(1)) for m in re.finditer(r"Index (\d+): SECONDARY", info)
                       if int(m.group(1)) >= keep}, reverse=True)
    for idx in residual:   # du plus haut au plus bas (les canaux sont contigus)
        cli.apply(["--ch-index", str(idx), "--ch-del"], f"Suppression du canal résiduel {idx}…")
    set_public_primary(cli)
    if profile.meshbridge:
        add_meshbridge_channel(cli)


def set_public_primary(cli):
    """Canal 0 = public suisse par défaut. La fréquence LoRa dérive du nom du
    canal primary : sur le public, le nœud est sur la fréquence du mesh suisse
    et les autres nœuds relaient son trafic (rebonds = portée)."""
    cli.apply(["--ch-index", "0", "--ch-set", "psk", "default", "--ch-set", "name", ""],
              "Canal 0 → public suisse (fréquence du mesh, rebonds)…")


def add_meshbridge_channel(cli):
    """Canal 1 = MeshBridge chiffré. Les relais publics transportent ces
    paquets sans pouvoir les lire : portée ET confidentialité."""
    psk = os.environ["MESHBRIDGE_PSK"].strip()
    cli.apply([
        "--ch-index", str(MESHBRIDGE_INDEX),
        "--ch-set", "name", "MeshBridge",
        "--ch-set", "psk", f"base64:{psk}",
        "--ch-set", "uplink_enabled", "false",
        "--ch-set", "downlink_enabled", "false",
    ], "Canal 1 → MeshBridge (chiffré)…")


def deploy(cli, profile):
    """Déploiement complet : écriture du profil (qui remet aussi les canaux à
    neuf) → vide la NodeDB → relecture."""
    try:
        write_profile(cli, profile)
        # Vide la liste des nœuds connus : après un changement de fréquence,
        # les anciennes entrées sont trompeuses. Elle se repeuple à l'écoute.
        cli.apply(["--reset-nodedb"], "Réinitialisation de la liste des nœuds connus…")

        conforme, gaps = verify(cli, profile)
        # Boucle de convergence : un nœud occupé peut perdre des écritures
        # malgré tout. Plutôt que tout redéployer, on rejoue UNIQUEMENT ce
        # qui est en écart, puis on revérifie (deux passes maximum).
        for _ in range(2):
            if conforme or not gaps:
                break
            fix_gaps(cli, profile, gaps)
            conforme, gaps = verify(cli, profile)

        if profile.meshbridge:
            show_fingerprint(cli)
        print("")
        if conforme:
            print(f"  \033[92m✅ '{profile.long_name}' déployé et vérifié.\033[0m")
            print("  \033[90m• Note Wio Tracker/nRF52 : si les boutons ou l'écran semblent figés,")
            print("    débranchez le câble USB et faites un appui court sur RESET.\033[0m")
        else:
            print(f"  \033[91m⚠ '{profile.long_name}' déployé mais NON conforme.\033[0m")
        return conforme
    except Exception as e:
        fail(f"Déploiement interrompu : {e}")
        return False


def fix_gaps(cli, profile, gaps):
    """Rejoue uniquement ce qui est en écart, chacun par son canal propre :
    l'identité via --set-owner, les réglages via --set."""
    settings = [(field, value) for field, value in gaps if field != "owner"]
    if len(settings) < len(gaps):
        cli.apply(["--set-owner", profile.long_name,
                   "--set-owner-short", profile.short_name],
                  "Correction de l'identité…")
    if settings:
        cli.apply(set_args(settings),
                  f"Correction ciblée de {len(settings)} réglage(s)…",
                  cli_timeout=180)


def read_settings(cli, fields):
    """Relit tous les champs en UNE invocation de la CLI (un --get par champ) :
    un seul aller-retour série au lieu d'un par champ."""
    args = []
    for field in fields:
        args += ["--get", field]
    out = cli.run(args)
    values = {}
    for field in fields:
        m = re.search(rf"^\s*{re.escape(field)}\s*:\s*(.+?)\s*$", out, re.I | re.M)
        if m:
            values[field] = m.group(1).strip()
    return values


def check_field(field, expected, actual):
    """Compare la valeur relue à l'attendue et affiche le verdict. Insensible
    à la casse : la CLI renvoie 'False' pour un 'false' écrit."""
    if actual is None:
        fail(f"{field:<44} = ILLISIBLE")
        return False
    shown = ENUMS.get(field, {}).get(actual, actual)
    if shown.lower() == expected.lower():
        ok(f"{field:<44} = {shown}")
        return True
    fail(f"{field:<44} = {shown}  (attendu : {expected})")
    return False


def check_owner(info, profile):
    """Vérifie le nom du nœud depuis --info : --set-owner peut se perdre
    silencieusement (vu sur le Heltec) et n'est pas relisible par --get."""
    m = re.search(r"^Owner\s*:\s*(.+?)\s*$", info, re.M)
    actual = m.group(1) if m else None
    expected = f"{profile.long_name} ({profile.short_name})"
    if actual == expected:
        ok(f"{'Owner':<44} = {actual}")
        return True
    fail(f"{'Owner':<44} = {actual or 'introuvable'}  (attendu : {expected})")
    return False


def check_channels(info, strict):
    """Contrôle la table des canaux : canal 0 public, et en mode strict le
    canal 1 MeshBridge sans aucun canal résiduel. Un booléen par contrôle."""
    results = []

    line0 = next((l for l in info.splitlines() if "Index 0: PRIMARY" in l), None)
    public = False
    if line0:
        m = re.search(r'"name":\s*"([^"]*)"', line0)
        name0 = m.group(1) if m else ""
        public = name0 == "" and "psk=secret" not in line0
    if public:
        ok(f"{'Canal 0':<44} = public (défaut)")
    else:
        fail(f"{'Canal 0':<44} = {'personnalisé' if line0 else 'introuvable'}  (attendu : public)")
    results.append(public)

    if not strict:
        return results

    line1 = next((l for l in info.splitlines() if "Index 1: SECONDARY" in l), None)
    has_bridge = bool(line1 and '"name": "MeshBridge"' in line1)
    if has_bridge:
        ok(f"{'Canal 1':<44} = MeshBridge")
    else:
        fail(f"{'Canal 1':<44} = absent/différent  (attendu : MeshBridge)")
    results.append(has_bridge)

    extra = sorted({int(m.group(1)) for m in re.finditer(r"Index (\d+): SECONDARY", info)
                    if int(m.group(1)) >= 2})
    if extra:
        fail(f"{'Canaux résiduels':<44} = index {extra}  (attendu : aucun)")
    else:
        ok(f"{'Canaux résiduels':<44} = aucun")
    results.append(not extra)
    return results


def verify(cli, profile):
    """Relit l'état désiré champ par champ. La liste vient de profile.settings(),
    la même que celle écrite : la vérification ne peut pas diverger de l'écriture.
    Renvoie (conforme, gaps) où gaps liste les (champ, valeur) en écart, à
    rejouer par fix_gaps — l'identité y figure sous le pseudo-champ 'owner'.
    (gaps vide si seuls les canaux posent problème : pas corrigeable champ
    par champ.)"""
    title("Vérification (relecture réelle des champs)")
    wanted = profile.settings()
    actual = read_settings(cli, [field for field, _ in wanted])
    results, gaps = [], []
    for field, expected in wanted:
        good = check_field(field, expected, actual.get(field))
        results.append(good)
        if not good:
            gaps.append((field, expected))

    info = cli.run(["--info"])
    owner_ok = check_owner(info, profile)
    results.append(owner_ok)
    if not owner_ok:
        gaps.append(("owner", profile.long_name))
    results += check_channels(info, strict=profile.meshbridge)

    passed, total = sum(results), len(results)
    print("")
    if passed == total:
        print(f"  \033[92m✅ {passed}/{total} — nœud conforme.\033[0m")
    else:
        print(f"  \033[91m❌ {passed}/{total} — {total - passed} à corriger ci-dessus.\033[0m")
    return passed == total, gaps


def show_fingerprint(cli):
    """Empreinte du canal (PAS l'URL : elle contient le PSK en clair)."""
    title("Empreinte des canaux (doit être IDENTIQUE entre Pierre et Paul)")
    for line in cli.run(["--info"]).splitlines():
        if "Complete URL" in line:
            m = re.search(r"https://\S+", line)
            if m:
                print(f"  Empreinte : \033[97m{hashlib.sha256(m.group(0).encode()).hexdigest()[:12]}\033[0m")
                return
    fail("URL de canal introuvable")

# ======================================================================
#  5. ASSISTANT INTERACTIF
# ======================================================================
def wait_for_single_node(cli):
    """Attend un ou plusieurs nœuds accessibles en USB et l'épingle."""
    while True:
        ports = meshtastic_ports()
        if len(ports) == 1:
            port = ports[0]
            if not os.access(port, os.R_OK | os.W_OK):
                fail(f"Pas les droits sur {port}.")
                print(f"\033[93m  sudo usermod -a -G dialout {getpass.getuser()}"
                      f"  (puis se déconnecter/reconnecter)\033[0m")
                print(f"\033[93m  ou : sg dialout -c \"python3 {sys.argv[0]}\"\033[0m")
                sys.exit(1)
            cli.port = port
            ok(f"Nœud détecté sur {cli.port}")
            return
        elif len(ports) > 1:
            print(f"\n\033[93mPlusieurs ports série détectés :\033[0m")
            for idx, p in enumerate(ports):
                print(f"    {idx + 1}. {p}")
            print(f"    {len(ports) + 1}. Rafraîchir la liste")
            rep = input(f"  Choisir le port (1-{len(ports) + 1}, Entrée par défaut pour rafraîchir) : ").strip()
            if rep.isdigit():
                val = int(rep)
                if 1 <= val <= len(ports):
                    port = ports[val - 1]
                    if not os.access(port, os.R_OK | os.W_OK):
                        fail(f"Pas les droits sur {port}.")
                        continue
                    cli.port = port
                    ok(f"Port sélectionné : {cli.port}")
                    return
        else:
            input("\n\033[93mAucun nœud. Brancher un nœud en USB, puis Entrée…\033[0m ")


PORT_BUSY_SIGN = "multiple access on port"   # texte exact renvoyé par la CLI meshtastic


def probe_node(cli):
    """(parle_meshtastic, version, nom_owner, port_occupe). Une seule
    interrogation, 20 s max. Un handshake muet = firmware étranger (MeshCore),
    boot en cours, OU port déjà ouvert par une autre appli (ex. l'appli
    Meshtastic desktop) — ce dernier cas est détecté explicitement (port_occupe)."""
    out = cli.try_run(["--info"], timeout=20)
    if out and "Owner" in out:
        v = re.search(r'firmware_?version[^0-9]*(\d+\.\d+\.\d+)', out, re.I)
        o = re.search(r"^Owner\s*:\s*(.+)$", out, re.M)
        owner = re.sub(r"\s*\(.*\)$", "", o.group(1)).strip() if o else None
        return (True, v.group(1) if v else None, owner, False)
    busy = bool(out) and PORT_BUSY_SIGN in out.lower()
    return (False, None, None, busy)


def latest_stable_firmware():
    """Dernière version stable (API GitHub). None si hors-ligne."""
    try:
        import json, urllib.request
        url = "https://api.github.com/repos/meshtastic/firmware/releases/latest"
        with urllib.request.urlopen(url, timeout=5) as r:
            tag = json.load(r).get("tag_name", "")
        m = re.search(r"(\d+\.\d+\.\d+)", tag)
        return m.group(1) if m else None
    except Exception:
        return None


def enable_ble_pierre(cli):
    title("Activation du BLE sur Pierre")
    pin = (os.environ.get("PIERRE_BLE_PIN") or "").strip()
    if not re.fullmatch(r"\d{6}", pin):
        fail("PIERRE_BLE_PIN doit être un PIN à 6 chiffres dans .env")
        return
    cli.apply([
        "--set", "bluetooth.enabled", "true",
        "--set", "bluetooth.mode", "FixedPin",
        "--set", "bluetooth.fixed_pin", pin,
    ], f"Activation BLE (PIN {pin}, défini dans .env)…")
    ok("BLE activé sur Pierre.")
    print("\n\033[93mCôté Raspberry Pi :\033[0m")
    print("  1) Débrancher Pierre, l'alimenter près de la fenêtre.")
    print("  2) bluetoothctl → scan on → repérer 'Meshtastic_*'")
    print(f"  3) pair / trust avec le PIN {pin}")
    print("  4) Reporter la MAC dans PIERRE_BLE_MAC (.env du Pi)")
    print("  5) Installer le bridge : README, étapes 3 et 4.")


def choose_standard_role(mobile):
    """Règle Netiquette : nœud transporté OU zone dense → CLIENT_MUTE ;
    zone peu couverte → CLIENT (le nœud aide à relayer)."""
    if mobile:
        return "CLIENT_MUTE"
    dense = ask_yes_no("  Le mesh est-il déjà dense ici (>50 nœuds dans l'app) ?")
    if dense:
        return "CLIENT_MUTE"
    print("  \033[90m(zone peu couverte → CLIENT : le nœud aidera à relayer)\033[0m")
    return "CLIENT"


def ask_role():
    """Menu de rôle : renvoie le NodeProfile choisi."""
    print("  Quel rôle attribuer à ce nœud ?")
    print(f"    1. {MESHBRIDGE_NODES['Maison'].long_name}  — fixe, relié au Raspberry Pi (passerelle)")
    print(f"    2. {MESHBRIDGE_NODES['Portable'].long_name}  — portable, accompagne le téléphone")
    print("    3. Nœud standard  — hors MeshBridge, conforme Netiquette (canal public)")
    while True:
        c = input("  Choix (1/2/3) : ").strip()
        if c in ("1", "2", "3"):
            break
    if c == "1":
        return MESHBRIDGE_NODES["Maison"]
    if c == "2":
        return MESHBRIDGE_NODES["Portable"]
    long_name = ""
    while not long_name:
        long_name = input("  Nom long (neutre de préférence) : ").strip()
    short_name = ask_emoji()      # emoji = avatar du nœud dans les applis
    while not short_name:
        s = input("  Nom court (2 à 4 caractères) : ").strip()
        if len(s.encode()) > 4:
            # len() compte les code points, pas les octets : un drapeau 🇨🇭 (2 code
            # points, 8 octets) passerait le test 2-4 mais serait refusé par le nœud.
            print("  \033[93mTrop long pour le firmware (4 octets max) — le nœud le refuserait.\033[0m")
        elif 2 <= len(s) <= 4:
            short_name = s
    mobile = ask_yes_no("  Nœud mobile (sac, voiture) plutôt que fixe ?")
    return standard_profile(long_name, short_name, mobile, choose_standard_role(mobile))


def ask_emoji():
    """Emoji optionnel utilisé comme NOM COURT : les applis Meshtastic
    l'affichent comme avatar du nœud. Limite firmware : 4 octets UTF-8 —
    les emoji composés (drapeaux, variantes) débordent et sont refusés.
    (Sur l'écran OLED des cartes, l'emoji s'affichera mal : compromis assumé.)"""
    print("  Utiliser un emoji comme nom court ? (affiché comme avatar dans l'appli)")
    print("    1. Aucun (Entrée) — saisir un nom court classique")
    print("    2. 🥾 Randonneur")
    print("    3. 🚗 Véhicule")
    print("    4. 🏠 Fixe")
    print("    5. 📡 Relais")
    print("    6. Autre (saisir un emoji)")
    presets = {"2": "🥾", "3": "🚗", "4": "🏠", "5": "📡"}
    while True:
        c = input("  Choix (1-6, Entrée = 1) : ").strip()
        if not c or c == "1":
            return ""
        if c in presets:
            return presets[c]
        if c == "6":
            c = input("  Emoji : ").strip()
        if c and not c.isascii():        # emoji tapé directement (menu ou option 6)
            if len(c.encode()) <= 4:
                return c
            print("  \033[93mEmoji trop long pour le firmware (4 octets max) — drapeaux et variantes ne tiennent pas.\033[0m")
        else:
            print("  \033[93mChoix non reconnu.\033[0m")


def ask_modem_preset():
    """Demande à l'utilisateur de choisir le modem preset. Les deux nœuds
    MeshBridge doivent recevoir le même (sinon les radios ne s'entendent plus)."""
    print("  Quel modem preset utiliser ? (le même pour les deux nœuds MeshBridge)")
    print("    1. MEDIUM_FAST (norme Netiquette Suisse — 3 rebonds)")
    print("    2. LONG_FAST (HORS Netiquette — majoritaire en pratique, portée maximale — 5 rebonds)")
    while True:
        rep = input("  Choix (1/2, Entrée par défaut) : ").strip()
        if not rep or rep == "1":
            print("  [i] Option MEDIUM_FAST sélectionnée : lora.hop_limit configuré à 3 rebonds.")
            return "MEDIUM_FAST"
        if rep == "2" or rep.lower() in ("l", "long", "long_fast"):
            print("  [i] Option LONG_FAST sélectionnée : lora.hop_limit configuré à 5 rebonds (hors Netiquette).")
            return "LONG_FAST"


def ask_action():
    """Menu d'action : déployer (écrit) ou vérifier seulement (aucune écriture)."""
    print("  Que faire avec ce profil ?")
    print("    1. Déployer (écrit la configuration sur le nœud)")
    print("    2. Vérifier seulement (aucune écriture, compare au profil choisi)")
    while True:
        c = input("  Choix (1/2, Entrée = 1) : ").strip()
        if not c or c == "1":
            return "deploy"
        if c == "2":
            return "check"


def check_only(cli, profile):
    """Relit le nœud et le compare au profil choisi, sans rien écrire."""
    try:
        conforme, _ = verify(cli, profile)
    except Exception as e:
        fail(f"Vérification interrompue : {e}")
        return
    print("")
    if conforme:
        print(f"  \033[92m✅ '{profile.long_name}' déjà conforme au profil choisi.\033[0m")
    else:
        print(f"  \033[91m⚠ '{profile.long_name}' NE correspond PAS au profil choisi (voir ci-dessus).\033[0m")


def assist_node(cli):
    """Prend en charge le nœud branché, de bout en bout."""
    wait_for_single_node(cli)

    step("Identification du firmware…")
    is_mesh, version, owner, port_busy = probe_node(cli)
    if not is_mesh:
        fail("Pas de réponse au protocole Meshtastic.")
        if port_busy:
            print("\033[93m  • Le port est déjà utilisé par une autre application (ex. l'appli Meshtastic desktop,")
            print("\033[93m    un moniteur série…) — un seul programme peut parler au port à la fois.\033[0m")
            print("\033[93m    Fermez-la puis réessayez (pas besoin de débrancher/rebrancher le nœud).\033[0m")
        else:
            print("\033[93m  • Soit le nœud démarrait encore → réessayer.\033[0m")
            print("\033[93m  • Soit un autre firmware tourne (MeshCore ?) → le flasher :\033[0m")
            print("\033[93m       https://flasher.meshtastic.org\033[0m")
        return

    ok(f"Firmware Meshtastic {version or '(illisible)'}")
    latest = latest_stable_firmware()
    if version and latest and tuple(map(int, version.split("."))) < tuple(map(int, latest.split("."))):
        print(f"  \033[93m[i] {version} < dernière stable {latest} — mise à jour conseillée")
        print(f"      via https://flasher.meshtastic.org\033[0m")

    print(f"\n  Nom actuel du nœud : {owner or 'inconnu'}.")
    profile = ask_role()
    preset = ask_modem_preset()
    profile = replace(profile, modem_preset=preset)
    if ask_action() == "check":
        check_only(cli, profile)
        return
    deployed = deploy(cli, profile)

    if profile.long_name == MESHBRIDGE_NODES["Maison"].long_name and deployed \
            and ask_yes_no("\nActiver le BLE sur Pierre maintenant ?"):
        enable_ble_pierre(cli)


def load_env():
    """Charge le .env de la racine du repo dans os.environ."""
    root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env_path = os.path.join(root_dir, ".env")
    if os.path.exists(env_path):
        load_dotenv(dotenv_path=env_path)
        print(f"\033[90m[.env] Variables chargées depuis {env_path}\033[0m")
    else:
        print(f"\033[93m[!] .env introuvable ({env_path}) — copier .env.example en .env.\033[0m")


def main():
    load_env()
    print("\033[96m=================================================\033[0m")
    print("   \033[97mASSISTANT DE CONFIGURATION MESHBRIDGE\033[0m")
    print("   \033[90mNetiquette Suisse — Janvier 2026\033[0m")
    print("\033[96m=================================================\033[0m")

    if not os.environ.get("MESHBRIDGE_PSK", "").strip():
        fail("MESHBRIDGE_PSK manquant dans .env"); sys.exit(1)
    if not shutil.which("meshtastic"):
        fail("CLI 'meshtastic' introuvable : pip install meshtastic"); sys.exit(1)

    cli = MeshtasticCLI()
    while True:
        assist_node(cli)
        if not ask_yes_no("\nConfigurer un autre nœud ?"):
            break
        input("Débrancher le nœud actuel, brancher l'autre, puis Entrée… ")
    print("\nTerminé.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrompu.")
        sys.exit(0)
