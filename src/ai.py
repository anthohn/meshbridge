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


def _summarize_cloud(content: str, instruction: str) -> str | None:
    if not config.GEMINI_KEY:
        return None
    try:
        client = genai.Client(api_key=config.GEMINI_KEY)
        prompt = _PROMPT.format(instruction=instruction, content=content[:6000])
        resp = client.models.generate_content(model=config.GEMINI_MODEL,
                                               contents=prompt)
        return resp.text.strip()
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
            timeout=60,
        )
        r.raise_for_status()
        return r.json()["response"].strip()
    except Exception as e:
        log.warning(f"local (Ollama) indisponible : {e}")
        return None


def compress(content: str, instruction: str, mode: str = "auto") -> tuple[str, str]:
    """Résume `content` selon `instruction`. Renvoie (texte, source).

    mode : "auto"  → cascade cloud → local → brut ;
           "cloud" → Gemini uniquement (sinon brut) ;
           "local" → Ollama uniquement (sinon brut).
    """
    if mode != "local":
        out = _summarize_cloud(content, instruction)
        if out is not None:
            return out, "cloud"
    if mode != "cloud":
        out = _summarize_local(content, instruction)
        if out is not None:
            return out, "local"
    log.warning("aucune IA disponible → fallback brut")
    return content, "raw"
