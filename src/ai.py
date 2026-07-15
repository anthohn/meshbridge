"""
MeshBridge — Couche de compression sémantique par IA.
Cascade de résilience : cloud (Gemini) → local (Ollama) → texte brut.
compress() renvoie (texte, source) pour tracer d'où vient la réponse.
"""
import logging
import requests
from google import genai

import config

log = logging.getLogger("meshbridge.ai")

_PROMPT = ("{instruction}\nRéponds en moins de 180 caractères, sans préambule."
           "\n\n---\n{content}")

# Client Gemini créé au premier appel puis réutilisé (connexion TLS comprise)
_gemini_client = None


def _get_gemini_client():
    global _gemini_client
    if _gemini_client is None:
        _gemini_client = genai.Client(
            api_key=config.GEMINI_KEY,
            http_options={"timeout": config.GEMINI_TIMEOUT_S * 1000})
    return _gemini_client


def _summarize_cloud(content: str, instruction: str) -> str | None:
    if not config.GEMINI_KEY:
        return None
    try:
        client = _get_gemini_client()
        prompt = _PROMPT.format(instruction=instruction, content=content[:6000])
        resp = client.models.generate_content(model=config.GEMINI_MODEL,
                                               contents=prompt)
        # resp.text peut être None (réponse bloquée par les filtres de sécurité)
        text = (resp.text or "").strip()
        return text or None
    except Exception as e:
        log.warning(f"cloud (Gemini) indisponible : {e}")
        return None


def _summarize_local(content: str, instruction: str) -> str | None:
    try:
        r = requests.post(
            config.OLLAMA_URL,
            json={
                "model": config.OLLAMA_MODEL,
                "prompt": _PROMPT.format(instruction=instruction,
                                         content=content[:4000]),
                "stream": False,
            },
            timeout=config.OLLAMA_TIMEOUT_S,
        )
        r.raise_for_status()
        # Un petit modèle peut renvoyer une chaîne vide → traité comme un échec
        text = r.json().get("response", "").strip()
        return text or None
    except Exception as e:
        log.warning(f"local (Ollama) indisponible : {e}")
        return None


def compress(content: str, instruction: str, mode: str = "auto") -> tuple[str, str] | None:
    """Résume `content` selon `instruction`. Renvoie (texte, source) ou None si échec.

    mode : "auto"  → cascade cloud → local → échec ;
           "cloud" → Gemini uniquement (sinon échec) ;
           "local" → Ollama uniquement (sinon échec).
    """
    if mode != "local":
        out = _summarize_cloud(content, instruction)
        if out:                     # None (échec) ou "" (vide) → on continue
            return out, "cloud"
    if mode != "cloud":
        out = _summarize_local(content, instruction)
        if out:
            return out, "local"
    log.warning("aucune IA disponible")
    return None
