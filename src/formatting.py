"""
MeshBridge — Mise en forme des réponses pour LoRa.
Troncature au mot près (sûre en octets) + étiquette de source IA.
"""
from config import MAX_LEN

# Indique d'où vient la réponse en clair : [Gemini], [Ollama], [Brut]
SOURCE_TAG = {
    "cloud": "[Gemini]",   # Gemini (rapide, en ligne)
    "local": "[Ollama]",   # Ollama (hors-ligne, sur le Pi)
    "raw":   "[Brut]",     # aucune IA dispo → texte tronqué brut
}


def tag(text: str, source: str | None = None) -> str:
    """Préfixe la réponse du nom de la source IA."""
    lbl = SOURCE_TAG.get(source) if source else None
    return f"{lbl} {text}" if lbl else text


def trim(text: str, limit: int = MAX_LEN) -> str:
    """
    Réduit un texte pour tenir dans `limit` octets UTF-8.
    Coupe au dernier mot entier et ajoute "…" (jamais au milieu d'un mot
    ni d'un caractère multi-octets).
    """
    text = " ".join(text.split())            # normalise les espaces
    if len(text.encode("utf-8")) <= limit:
        return text

    cut = text.encode("utf-8")[: limit - 3]  # 3 octets réservés pour "…"
    trimmed = cut.decode("utf-8", errors="ignore")
    if " " in trimmed:                       # recule au dernier mot complet
        trimmed = trimmed.rsplit(" ", 1)[0]
    return trimmed + "…"
