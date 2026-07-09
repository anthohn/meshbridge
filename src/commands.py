"""
MeshBridge — Registre des commandes et répartiteur.
Chaque commande est préfixée par "/". Les handlers renvoient un Reply
(texte + source IA éventuelle). dispatch() gère parsing, erreurs et
mise en forme finale garantie < MAX_LEN octets.
"""
import re
import logging
import datetime
from dataclasses import dataclass
from typing import Callable

import requests

import config
from ai import compress
from formatting import trim, tag

log = logging.getLogger("meshbridge.cmd")


@dataclass
class Reply:
    text: str
    source: str | None = None      # "cloud" / "local" / "raw" / None


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
    url = f"https://wttr.in/{ville}?format=%l:+%c+%t+%w+%p&m"
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
    text, source = compress(" | ".join(titres),
                            "Résume ces titres d'actualité en français, format [1]...[2]...[3].",
                            mode)
    return Reply(text, source)


def cmd_web(arg: str, mode: str) -> Reply:
    url = arg.strip()
    if not url:
        return Reply("Usage: /web <url>")
    if not url.startswith("http"):
        url = "https://" + url
    r = requests.get(url, timeout=config.HTTP_TIMEOUT,
                     headers={"User-Agent": "MeshBridge/1.0"})
    r.raise_for_status()
    html = re.sub(r"<script.*?</script>", " ", r.text, flags=re.S | re.I)
    html = re.sub(r"<style.*?</style>", " ", html, flags=re.S | re.I)
    html = re.sub(r"<[^>]+>", " ", html)
    text, source = compress(" ".join(html.split()),
                            f"Résume l'essentiel de cette page web ({url}).",
                            mode)
    return Reply(text, source)


def cmd_ask(arg: str, mode: str) -> Reply:
    q = arg.strip()
    if not q:
        return Reply("Usage: /ask <question>")
    date = datetime.datetime.now().strftime("%d %B %Y")
    text, source = compress(
        q, f"Nous sommes le {date}. Réponds de façon factuelle et concise.",
        mode)
    return Reply(text, source)


def cmd_help(arg: str, mode: str) -> Reply:
    return Reply(HELP_TEXT)


# ---------------------------------------------------------------- registre
COMMANDS: dict[str, Command] = {
    "meteo": Command(cmd_meteo, "/meteo <ville>"),
    "news":  Command(cmd_news,  "/news",          slow=True),
    "web":   Command(cmd_web,   "/web <url>",     slow=True),
    "ask":   Command(cmd_ask,   "/ask <question>", slow=True),
    "ping":  Command(cmd_ping,  "/ping"),
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
        reply = cmd.handler(arg, mode)
    except Exception as e:
        log.error(f"/{verb} erreur : {e}")
        return trim(f"⚠️ erreur sur /{verb}")
    return trim(tag(reply.text, reply.source))
