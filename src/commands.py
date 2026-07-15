"""
MeshBridge — Registre des commandes et répartiteur.
Chaque commande est préfixée par "/". Les handlers renvoient un Reply
(texte + source IA éventuelle). dispatch() gère parsing, erreurs et
mise en forme finale garantie < MAX_LEN octets.
"""
import re
import time
import socket
import logging
import datetime
import ipaddress
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlparse, urljoin, quote

import requests

import config
import metrics
from ai import compress
from formatting import trim, tag

log = logging.getLogger("meshbridge.cmd")


@dataclass
class Reply:
    text: str
    source: str | None = None      # "cloud" / "local" / "raw" / None
    in_len: int = 0                # taille du contenu AVANT compression IA
                                   # (0 si pas d'IA) → taux de compression


@dataclass
class Command:
    handler: Callable[[str, str], Reply]   # (arg, mode IA) → Reply
    usage: str
    slow: bool = False             # True → accusé de réception + traité en tâche de fond


# ---------------------------------------------------------------- handlers
def cmd_ping(arg: str, mode: str) -> Reply:
    return Reply("pong ✅ relai actif")


def cmd_meteo(arg: str, mode: str) -> Reply:
    ville = arg.strip() or "Geneve"
    # quote() : une ville contenant "?", "#" ou "&" ne doit pas détourner l'URL
    url = f"https://wttr.in/{quote(ville)}?format=%l:+%c+%t+%w+%p&m"
    r = requests.get(url, timeout=config.HTTP_TIMEOUT)
    r.raise_for_status()
    return Reply(r.text.strip())   # source directe (API), pas d'IA


def cmd_news(arg: str, mode: str) -> Reply:
    ids = requests.get(
        "https://hacker-news.firebaseio.com/v0/topstories.json",
        timeout=config.HTTP_TIMEOUT,
    ).json()[:5]
    titres = []
    for sid in ids:
        item = requests.get(
            f"https://hacker-news.firebaseio.com/v0/item/{sid}.json",
            timeout=config.HTTP_TIMEOUT,
        ).json()
        titres.append(item.get("title", ""))
    contenu = " | ".join(titres)
    text, source = compress(contenu,
                            "Résume ces titres d'actualité en français, format [1]...[2]...[3].",
                            mode)
    return Reply(text, source, in_len=len(contenu))


def _assert_public_host(url: str) -> None:
    """Anti-SSRF : le Pi est une passerelle vers Internet, pas vers le LAN.
    Refuse loopback, réseaux privés, lien-local, etc. (toutes les résolutions)."""
    host = urlparse(url).hostname
    if not host:
        raise ValueError("URL invalide")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        raise ValueError(f"hôte introuvable : {host}")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise ValueError(f"adresse non publique refusée ({host})")


def _fetch_page(url: str) -> str:
    """GET contrôlé : schéma http(s) seul, hôte re-vérifié à chaque
    redirection, contenu textuel uniquement, taille plafonnée."""
    for _ in range(config.WEB_MAX_REDIRECTS + 1):
        if not re.match(r"^https?://", url, re.I):
            raise ValueError("seuls http:// et https:// sont acceptés")
        _assert_public_host(url)

        r = requests.get(url, timeout=config.HTTP_TIMEOUT, stream=True,
                         allow_redirects=False,
                         headers={"User-Agent": "MeshBridge/1.0"})
        if r.is_redirect or r.is_permanent_redirect:
            url = urljoin(url, r.headers.get("Location", ""))
            continue
        r.raise_for_status()

        ctype = r.headers.get("Content-Type", "text/html")
        if not any(t in ctype for t in ("text", "html", "xml", "json")):
            raise ValueError(f"contenu non textuel ({ctype.split(';')[0]})")

        chunks, size = [], 0
        for chunk in r.iter_content(chunk_size=8192):
            chunks.append(chunk)
            size += len(chunk)
            if size >= config.WEB_MAX_BYTES:   # on résume le début, inutile de tout charger
                break
        return b"".join(chunks).decode(r.encoding or "utf-8", errors="ignore")

    raise ValueError("trop de redirections")


def cmd_web(arg: str, mode: str) -> Reply:
    url = arg.strip()
    if not url:
        return Reply("Usage: /web <url>")
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    try:
        page = _fetch_page(url)
    except ValueError as e:
        return Reply(f"⚠️ {e}")
    html = re.sub(r"<script.*?</script>", " ", page, flags=re.S | re.I)
    html = re.sub(r"<style.*?</style>", " ", html, flags=re.S | re.I)
    html = re.sub(r"<[^>]+>", " ", html)
    contenu = " ".join(html.split())
    text, source = compress(contenu,
                            f"Résume l'essentiel de cette page web ({url}).",
                            mode)
    return Reply(text, source, in_len=len(contenu))


# strftime("%B") dépend de la locale système (anglais par défaut sur un Pi)
_MOIS_FR = ("janvier", "février", "mars", "avril", "mai", "juin", "juillet",
            "août", "septembre", "octobre", "novembre", "décembre")


def cmd_ask(arg: str, mode: str) -> Reply:
    q = arg.strip()
    if not q:
        return Reply("Usage: /ask <question>")
    now = datetime.datetime.now()
    date = f"{now.day} {_MOIS_FR[now.month - 1]} {now.year}"
    text, source = compress(
        q, f"Nous sommes le {date}. Réponds de façon factuelle et concise.",
        mode)
    if source == "raw":
        # Le fallback brut renverrait la question elle-même à l'envoyeur
        return Reply("⚠️ aucune IA disponible — réessayer plus tard")
    return Reply(text, source, in_len=len(q))


def cmd_stats(arg: str, mode: str) -> Reply:
    return Reply(metrics.summary())


def cmd_help(arg: str, mode: str) -> Reply:
    return Reply(HELP_TEXT)


# ---------------------------------------------------------------- registre
COMMANDS: dict[str, Command] = {
    "meteo": Command(cmd_meteo, "/meteo <ville>", slow=True),   # appel réseau (wttr.in)
    "news":  Command(cmd_news,  "/news",          slow=True),
    "web":   Command(cmd_web,   "/web <url>",     slow=True),
    "ask":   Command(cmd_ask,   "/ask <question>", slow=True),
    "ping":  Command(cmd_ping,  "/ping"),
    "stats": Command(cmd_stats, "/stats"),
    "help":  Command(cmd_help,  "/help"),
}

# Commandes lentes (IA/scraping) → méritent un accusé de réception
SLOW = {name for name, c in COMMANDS.items() if c.slow}

# /help généré automatiquement depuis le registre (reste court pour LoRa)
HELP_TEXT = "Cmds: " + " | ".join(c.usage for c in COMMANDS.values())


# ---------------------------------------------------------------- dispatch
# Suffixe optionnel "!local" / "!cloud" en fin de commande → surcharge AI_MODE
_MODE_RE = re.compile(r"\s*!(local|cloud)\s*$", re.IGNORECASE)


def dispatch(verb: str, arg: str) -> str:
    """Exécute une commande déjà parsée et renvoie le texte final (< MAX_LEN)."""
    cmd = COMMANDS.get(verb)
    if cmd is None:
        return trim(f"❓ /{verb} inconnu. Tape /help")

    mode = config.AI_MODE
    m = _MODE_RE.search(arg)
    if m:
        mode = m.group(1).lower()
        arg = arg[:m.start()].strip()

    try:
        log.info(f"exec /{verb} {arg!r} (ia={mode})")
        t0 = time.time()
        reply = cmd.handler(arg, mode)
    except Exception as e:
        log.error(f"/{verb} erreur : {e}")
        return trim(f"⚠️ erreur sur /{verb}")

    final = trim(tag(reply.text, reply.source))
    metrics.record(verb, mode, reply.source, reply.in_len,
                   len(final.encode("utf-8")), time.time() - t0)
    return final
