"""Tests de la cascade IA : cloud → local → brut, et les modes forcés."""
import ai


def test_auto_prend_le_cloud_en_premier(monkeypatch):
    monkeypatch.setattr(ai, "_summarize_cloud", lambda c, i: "vu par gemini")
    monkeypatch.setattr(ai, "_summarize_local", lambda c, i: "vu par ollama")
    assert ai.compress("x", "instruction") == ("vu par gemini", "cloud")


def test_auto_bascule_sur_local_si_cloud_en_panne(monkeypatch):
    monkeypatch.setattr(ai, "_summarize_cloud", lambda c, i: None)
    monkeypatch.setattr(ai, "_summarize_local", lambda c, i: "vu par ollama")
    assert ai.compress("x", "instruction") == ("vu par ollama", "local")


def test_fallback_brut_si_tout_est_en_panne(monkeypatch):
    monkeypatch.setattr(ai, "_summarize_cloud", lambda c, i: None)
    monkeypatch.setattr(ai, "_summarize_local", lambda c, i: None)
    assert ai.compress("contenu original", "instruction") is None


def test_reponse_vide_traitee_comme_une_panne(monkeypatch):
    # un backend qui répond "" ne doit pas produire un message vide sur la radio
    monkeypatch.setattr(ai, "_summarize_cloud", lambda c, i: "")
    monkeypatch.setattr(ai, "_summarize_local", lambda c, i: "secours")
    assert ai.compress("x", "instruction") == ("secours", "local")


def test_mode_local_n_appelle_jamais_le_cloud(monkeypatch):
    appels = []
    monkeypatch.setattr(ai, "_summarize_cloud",
                        lambda c, i: appels.append("cloud") or "gemini")
    monkeypatch.setattr(ai, "_summarize_local", lambda c, i: "ollama")
    assert ai.compress("x", "instruction", "local") == ("ollama", "local")
    assert appels == []


def test_mode_cloud_en_panne_va_direct_au_brut(monkeypatch):
    # cloud forcé : pas de détour par Ollama, on assume l'échec
    monkeypatch.setattr(ai, "_summarize_cloud", lambda c, i: None)
    monkeypatch.setattr(ai, "_summarize_local", lambda c, i: "ollama")
    assert ai.compress("brut", "instruction", "cloud") is None


def test_gemini_reponse_bloquee_renvoie_none(monkeypatch):
    # resp.text vaut None quand Gemini bloque la réponse (filtres de sécurité)
    class FauxResp:
        text = None

    class FauxModels:
        def generate_content(self, **kwargs):
            return FauxResp()

    class FauxClient:
        def __init__(self, **kwargs):
            self.models = FauxModels()

    import config
    monkeypatch.setattr(config, "GEMINI_KEY", "cle-de-test")
    monkeypatch.setattr(ai.genai, "Client", FauxClient, raising=False)
    assert ai._summarize_cloud("contenu", "instruction") is None
