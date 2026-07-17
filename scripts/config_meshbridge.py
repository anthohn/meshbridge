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
# Nom court = emoji : affiché comme avatar du nœud par les applis Meshtastic.
MESHBRIDGE_NODES = {
    "Maison":   NodeProfile("Pierre", "📡", "CLIENT", "ALL",
                            pos_secs="86400", fixed="true", meshbridge=True),
    "Portable": NodeProfile("Paul", "🎒", "CLIENT_MUTE", "LOCAL_ONLY",
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
#  2. CONSOLE — tout l'affichage passe par ces helpers : glyphes de
#     statut à largeur fixe, un seul format de prompt pour tous les menus.
# ======================================================================
GRIS, VERT, ROUGE, JAUNE, BLEU, BLANC, FIN = ("\033[90m", "\033[92m", "\033[91m",
                                              "\033[93m", "\033[94m", "\033[97m", "\033[0m")

def step(m):    print(f"  {GRIS}→  {m}{FIN}")
def ok(m):      print(f"  {VERT}✓{FIN}  {m}")
def fail(m):    print(f"  {ROUGE}✗{FIN}  {m}")
def info(m):    print(f"  {BLEU}i{FIN}  {m}")
def warn(m):    print(f"  {JAUNE}!  {m}{FIN}")
def section(t): print(f"\n{BLEU}── {t} {'─' * max(0, 44 - len(t))}{FIN}\n")


def banner():
    l1, l2 = "MeshBridge · assistant de configuration", "Netiquette Suisse — janvier 2026"
    print(f"\n{BLEU}╭{'─' * 46}╮{FIN}")
    print(f"{BLEU}│{FIN}  {BLANC}{l1:<44}{FIN}{BLEU}│{FIN}")
    print(f"{BLEU}│{FIN}  {GRIS}{l2:<44}{FIN}{BLEU}│{FIN}")
    print(f"{BLEU}╰{'─' * 46}╯{FIN}")


def menu(titre, options, note=None):
    """Le seul moteur de menu du script : options alignées, prompt et
    message d'erreur identiques partout — un choix explicite est requis.
    options = [(label, description), …] ; renvoie le numéro choisi (1-N)."""
    section(titre)
    if note:
        print(f"  {GRIS}{note}{FIN}\n")
    aligne = any(desc for _, desc in options)   # colonne alignée seulement si des descriptions existent
    larg = max(len(label) for label, _ in options)
    for i, (label, desc) in enumerate(options, 1):
        label_aff = f"{label:<{larg}}" if aligne else label
        suffixe = f"  {GRIS}{desc}{FIN}" if desc else ""
        print(f"  {BLEU}{i} ›{FIN} {BLANC}{label_aff}{FIN}{suffixe}")
    borne = f"1-{len(options)}"
    while True:
        c = input(f"\n  Choix {borne} : ").strip()
        if c.isdigit() and 1 <= int(c) <= len(options):
            return int(c)
        warn(f"Saisie non reconnue — attendu : {borne}.")


def ask_yes_no(question):
    """Oui/Non via le MÊME moteur que les autres menus : un seul type
    d'interaction dans tout le script (toujours un numéro à saisir)."""
    return menu(question, [("Oui", ""), ("Non", "")]) == 1

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
                step(f"Nœud injoignable (délai dépassé), nouvel essai… ({attempt}/15)")
                self.wait_until_ready()      # ré-épingle le port s'il a changé
            except subprocess.CalledProcessError as e:
                out = (e.stdout or "") + (e.stderr or "") or str(e)
                if not any(s in out.lower() for s in self.REBOOT_SIGNS):
                    raise RuntimeError(f"meshtastic {' '.join(args)} a échoué :\n{out}")
                if attempt == 1:
                    cause = next((l.strip() for l in out.splitlines()
                                  if any(s in l.lower() for s in self.REBOOT_SIGNS)), "")
                    print(f"  {GRIS}(cause : {cause[:88]}){FIN}")
                step(f"Nœud injoignable (reboot ?), nouvel essai… ({attempt}/15)")
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
            warn("Le nœud ne répond plus (certaines cartes gèlent sur une écriture).")
            input(f"  {JAUNE}Le redémarrer avec son bouton (appui long), puis Entrée… {FIN}")
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
            print(f"  {VERT}✓ '{profile.long_name}' déployé et vérifié.{FIN}")
            print(f"  {GRIS}Note Wio Tracker/nRF52 : si les boutons ou l'écran semblent figés,")
            print(f"    débrancher le câble USB et faire un appui court sur RESET.{FIN}")
        else:
            print(f"  {ROUGE}✗ '{profile.long_name}' déployé mais NON conforme.{FIN}")
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
    section("Vérification (relecture réelle)")
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
        print(f"  {VERT}✓ {passed}/{total} — nœud conforme.{FIN}")
    else:
        print(f"  {ROUGE}✗ {passed}/{total} — {total - passed} écart(s) ci-dessus.{FIN}")
    return passed == total, gaps


def show_fingerprint(cli):
    """Empreinte du canal (PAS l'URL : elle contient le PSK en clair)."""
    section("Empreinte des canaux")
    print(f"  {GRIS}Doit être identique entre Pierre et Paul.{FIN}")
    for line in cli.run(["--info"]).splitlines():
        if "Complete URL" in line:
            m = re.search(r"https://\S+", line)
            if m:
                print(f"  Empreinte : {BLANC}{hashlib.sha256(m.group(0).encode()).hexdigest()[:12]}{FIN}")
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
                warn(f"sudo usermod -a -G dialout {getpass.getuser()}  (puis se déconnecter/reconnecter)")
                warn(f"ou : sg dialout -c \"python3 {sys.argv[0]}\"")
                sys.exit(1)
            cli.port = port
            ok(f"Nœud détecté sur {cli.port}")
            return
        elif len(ports) > 1:
            options = [(p, "") for p in ports] + [("Rafraîchir la liste", "")]
            c = menu("Plusieurs ports série détectés", options)
            if c < len(options):
                port = ports[c - 1]
                if not os.access(port, os.R_OK | os.W_OK):
                    fail(f"Pas les droits sur {port}.")
                    continue
                cli.port = port
                ok(f"Port sélectionné : {cli.port}")
                return
        else:
            input(f"\n  {JAUNE}Aucun nœud. Brancher un nœud en USB, puis Entrée…{FIN} ")


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
    section("Activation du BLE sur Pierre")
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
    print(f"\n  {JAUNE}Côté Raspberry Pi :{FIN}")
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
    dense = menu("Densité du mesh ici", [
        ("Dense", "déjà beaucoup de nœuds (>50 dans l'app)"),
        ("Peu couvert", "peu de nœuds — ce nœud aidera à relayer"),
    ]) == 1
    return "CLIENT_MUTE" if dense else "CLIENT"


def ask_role():
    """Menu de rôle : renvoie le NodeProfile choisi."""
    c = menu("Rôle du nœud", [
        (MESHBRIDGE_NODES["Maison"].long_name, "fixe · passerelle vers le Raspberry Pi"),
        (MESHBRIDGE_NODES["Portable"].long_name, "portable · accompagne le téléphone"),
        ("Nœud standard", "hors MeshBridge · Netiquette, canal public"),
    ])
    if c == 1:
        return MESHBRIDGE_NODES["Maison"]
    if c == 2:
        return MESHBRIDGE_NODES["Portable"]
    section("Nœud standard")
    long_name = ""
    while not long_name:
        long_name = input("  Nom long (neutre de préférence) : ").strip()
    short_name = ask_emoji()      # emoji = avatar du nœud dans les applis
    while not short_name:
        s = input("  Nom court (2 à 4 caractères) : ").strip()
        if len(s.encode()) > 4:
            # len() compte les code points, pas les octets : un drapeau 🇨🇭 (2 code
            # points, 8 octets) passerait le test 2-4 mais serait refusé par le nœud.
            warn("Trop long pour le firmware (4 octets max) — le nœud le refuserait.")
        elif 2 <= len(s) <= 4:
            short_name = s
    mobile = menu("Usage du nœud", [
        ("Transporté", "sur soi, dans un sac, en voiture…"),
        ("Fixe", "reste toujours au même endroit"),
    ]) == 1
    return standard_profile(long_name, short_name, mobile, choose_standard_role(mobile))


def ask_emoji():
    """Emoji optionnel utilisé comme NOM COURT : les applis Meshtastic
    l'affichent comme avatar du nœud. Limite firmware : 4 octets UTF-8 —
    les emoji composés (drapeaux, variantes) débordent et sont refusés.
    (Sur l'écran OLED des cartes, l'emoji s'affichera mal : compromis assumé.)
    Question libre — un emoji tapé directement est accepté — donc pas de
    menu() ici, mais les mêmes codes visuels et le même format de prompt."""
    print(f"  Un emoji comme nom court ? {GRIS}(devient l'avatar du nœud dans l'appli){FIN}")
    opts = [("Aucun", "saisir un nom court classique"), ("🥾", "randonneur"),
            ("🚗", "véhicule"), ("🏠", "fixe"), ("📡", "relais"), ("Autre", "taper un emoji")]
    for i, (label, desc) in enumerate(opts, 1):
        print(f"  {BLEU}{i} ›{FIN} {BLANC}{label:<5}{FIN}  {GRIS}{desc}{FIN}")
    presets = {"2": "🥾", "3": "🚗", "4": "🏠", "5": "📡"}
    while True:
        c = input("\n  Choix 1-6 : ").strip()
        if c == "1":
            return ""
        if c in presets:
            return presets[c]
        if c == "6":
            c = input("  Emoji : ").strip()
        if c and not c.isascii():        # emoji tapé directement (menu ou option 6)
            if len(c.encode()) <= 4:
                return c
            warn("Emoji trop long pour le firmware (4 octets max) — drapeaux et variantes ne tiennent pas.")
        else:
            warn("Saisie non reconnue — attendu : 1-6 ou un emoji.")


def ask_modem_preset():
    """Demande à l'utilisateur de choisir le modem preset. Les deux nœuds
    MeshBridge doivent recevoir le même (sinon les radios ne s'entendent plus)."""
    c = menu("Modem preset", [
        ("MEDIUM_FAST", "norme Netiquette Suisse · 3 rebonds"),
        ("LONG_FAST", "hors Netiquette · majoritaire en pratique, portée maximale · 5 rebonds"),
    ], note="Le même pour les deux nœuds MeshBridge, sinon les radios ne s'entendent plus.")
    if c == 1:
        info("MEDIUM_FAST : lora.hop_limit configuré à 3 rebonds.")
        return "MEDIUM_FAST"
    info("LONG_FAST : lora.hop_limit configuré à 5 rebonds (hors Netiquette).")
    return "LONG_FAST"


def show_profile(profile):
    """Récapitulatif du profil retenu, juste avant de choisir quoi en faire."""
    section("Profil retenu")
    hop = "5" if profile.modem_preset == "LONG_FAST" else "3"
    canaux = "0 public · 1 MeshBridge" if profile.meshbridge else "0 public"
    print(f"  {GRIS}{'Nom':<8}{FIN}{profile.long_name} ({profile.short_name})")
    print(f"  {GRIS}{'Rôle':<8}{FIN}{profile.role} · rebroadcast {profile.rebroadcast}")
    print(f"  {GRIS}{'Radio':<8}{FIN}{profile.modem_preset} · EU_868 · {hop} rebonds")
    print(f"  {GRIS}{'Canaux':<8}{FIN}{canaux}")


def ask_action():
    """Menu d'action : déployer (écrit) ou vérifier seulement."""
    c = menu("Action", [
        ("Déployer", "écrit la configuration sur le nœud"),
        ("Vérifier seulement", "compare au profil · ne corrige qu'avec accord"),
    ])
    return "deploy" if c == 1 else "check"


def check_only(cli, profile):
    """Relit le nœud et le compare au profil choisi. N'écrit rien, sauf si
    l'utilisateur accepte de corriger les écarts détectés — mêmes corrections
    ciblées que la boucle de convergence du déploiement (fix_gaps)."""
    try:
        conforme, gaps = verify(cli, profile)
    except Exception as e:
        fail(f"Vérification interrompue : {e}")
        return
    print("")
    if conforme:
        print(f"  {VERT}✓ '{profile.long_name}' déjà conforme au profil choisi.{FIN}")
        return
    print(f"  {ROUGE}✗ '{profile.long_name}' ne correspond pas au profil choisi (voir ci-dessus).{FIN}")
    if not gaps:
        # Seuls les canaux sont en écart : pas corrigeable champ par champ.
        warn("Écart sur les canaux : relancer en mode Déployer pour les remettre à neuf.")
        return
    if not ask_yes_no("Corriger ces écarts maintenant (écrit sur le nœud) ?"):
        return
    try:
        fix_gaps(cli, profile, gaps)
        conforme, _ = verify(cli, profile)
        print("")
        if conforme:
            print(f"  {VERT}✓ '{profile.long_name}' corrigé et conforme.{FIN}")
        else:
            print(f"  {ROUGE}✗ Toujours non conforme — relancer en mode Déployer.{FIN}")
    except Exception as e:
        fail(f"Correction interrompue : {e}")


def assist_node(cli):
    """Prend en charge le nœud branché, de bout en bout."""
    wait_for_single_node(cli)

    step("Identification du firmware…")
    is_mesh, version, owner, port_busy = probe_node(cli)
    if not is_mesh:
        fail("Pas de réponse au protocole Meshtastic.")
        if port_busy:
            warn("Le port est déjà utilisé par une autre application (appli Meshtastic")
            warn("desktop, moniteur série…) — un seul programme peut lui parler à la fois.")
            warn("La fermer puis réessayer (pas besoin de débrancher le nœud).")
        else:
            warn("Soit le nœud démarrait encore → réessayer.")
            warn("Soit un autre firmware tourne (MeshCore ?) → le flasher :")
            warn("    https://flasher.meshtastic.org")
        return

    ok(f"Firmware Meshtastic {version or '(illisible)'}")
    latest = latest_stable_firmware()
    if version and latest and tuple(map(int, version.split("."))) < tuple(map(int, latest.split("."))):
        warn(f"{version} < dernière stable {latest} — mise à jour conseillée via https://flasher.meshtastic.org")
    info(f"Nom actuel du nœud : {owner or 'inconnu'}")
    profile = ask_role()
    preset = ask_modem_preset()
    profile = replace(profile, modem_preset=preset)
    show_profile(profile)
    if ask_action() == "check":
        check_only(cli, profile)
        return
    deployed = deploy(cli, profile)

    if profile.long_name == MESHBRIDGE_NODES["Maison"].long_name and deployed:
        print("")
        if ask_yes_no("Activer le BLE sur Pierre maintenant ?"):
            enable_ble_pierre(cli)


def load_env():
    """Charge le .env de la racine du repo dans os.environ."""
    root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env_path = os.path.join(root_dir, ".env")
    if os.path.exists(env_path):
        load_dotenv(dotenv_path=env_path)
        print(f"{GRIS}.env chargé depuis {env_path}{FIN}")
    else:
        warn(f".env introuvable ({env_path}) — copier .env.example en .env.")


def main():
    load_env()
    banner()

    if not os.environ.get("MESHBRIDGE_PSK", "").strip():
        fail("MESHBRIDGE_PSK manquant dans .env"); sys.exit(1)
    if not shutil.which("meshtastic"):
        fail("CLI 'meshtastic' introuvable : pip install meshtastic"); sys.exit(1)

    cli = MeshtasticCLI()
    while True:
        assist_node(cli)
        print("")
        if not ask_yes_no("Configurer un autre nœud ?"):
            break
        input(f"  {GRIS}Débrancher le nœud actuel, brancher l'autre, puis Entrée…{FIN} ")
    print("\nTerminé.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrompu.")
        sys.exit(0)
