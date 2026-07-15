"""Tests de la mise en forme LoRa : troncature en octets + étiquette de source."""
from formatting import trim, tag


def test_texte_court_rendu_tel_quel():
    assert trim("bonjour") == "bonjour"


def test_espaces_normalises():
    assert trim("un   deux\n  trois") == "un deux trois"


def test_coupe_respecte_la_limite_en_octets():
    long_texte = "mot " * 100
    resultat = trim(long_texte, limit=50)
    assert len(resultat.encode("utf-8")) <= 50
    assert resultat.endswith("…")


def test_ne_coupe_jamais_un_mot_en_deux():
    resultat = trim("aaaa bbbb cccc dddd", limit=12)
    # 12 octets - 3 pour "…" = 9 → "aaaa bbbb" recule au dernier mot entier
    assert resultat == "aaaa…"


def test_accents_multi_octets_jamais_casses():
    # "é" fait 2 octets en UTF-8 : couper au milieu donnerait un caractère
    # invalide que l'app Meshtastic afficherait comme "?"
    texte = "café coûte très cher à Genève " * 10
    resultat = trim(texte, limit=33)
    assert len(resultat.encode("utf-8")) <= 33
    resultat.encode("utf-8").decode("utf-8")   # ne doit pas lever d'erreur


def test_tag_prefixe_selon_la_source():
    assert tag("texte", "cloud") == "[Gemini] texte"
    assert tag("texte", "local") == "[Ollama] texte"
    assert tag("texte", "raw") == "[Brut] texte"


def test_tag_sans_source_ne_change_rien():
    assert tag("texte", None) == "texte"
    assert tag("texte", "source-inconnue") == "texte"
