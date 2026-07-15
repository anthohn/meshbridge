"""Tests du répartiteur (dispatch) et des garde-fous de /web."""
import pytest

import config
import commands
from commands import dispatch, Reply, Command


def enregistre_fausse_commande(monkeypatch, recu):
    """Remplace /ask par un handler qui note ce qu'il reçoit."""
    def faux_handler(arg, mode):
        recu["arg"] = arg
        recu["mode"] = mode
        return Reply("ok")
    monkeypatch.setitem(commands.COMMANDS, "ask", Command(faux_handler, "/ask"))


# ---------------------------------------------------------------- dispatch
def test_commande_inconnue():
    assert "inconnu" in dispatch("nexistepas", "")


def test_suffixe_local_extrait_de_l_argument(monkeypatch):
    recu = {}
    enregistre_fausse_commande(monkeypatch, recu)
    dispatch("ask", "capitale du Japon !local")
    assert recu == {"arg": "capitale du Japon", "mode": "local"}


def test_suffixe_insensible_a_la_casse(monkeypatch):
    recu = {}
    enregistre_fausse_commande(monkeypatch, recu)
    dispatch("ask", "question !CLOUD")
    assert recu["mode"] == "cloud"


def test_sans_suffixe_on_garde_le_mode_par_defaut(monkeypatch):
    recu = {}
    enregistre_fausse_commande(monkeypatch, recu)
    dispatch("ask", "question sans suffixe")
    assert recu["mode"] == config.AI_MODE


def test_exclamation_au_milieu_n_est_pas_un_suffixe(monkeypatch):
    recu = {}
    enregistre_fausse_commande(monkeypatch, recu)
    dispatch("ask", "pourquoi !local ne marche pas ici ?")
    assert recu["arg"] == "pourquoi !local ne marche pas ici ?"
    assert recu["mode"] == config.AI_MODE


def test_handler_qui_plante_donne_un_message_propre(monkeypatch):
    def handler_casse(arg, mode):
        raise RuntimeError("boum")
    monkeypatch.setitem(commands.COMMANDS, "ask", Command(handler_casse, "/ask"))
    assert "erreur" in dispatch("ask", "x")


def test_reponse_toujours_sous_la_limite_lora(monkeypatch):
    def handler_bavard(arg, mode):
        return Reply("blabla " * 500, "cloud")
    monkeypatch.setitem(commands.COMMANDS, "ask", Command(handler_bavard, "/ask"))
    reponse = dispatch("ask", "x")
    assert len(reponse.encode("utf-8")) <= config.MAX_LEN


# ---------------------------------------------------------------- /web
def test_web_refuse_les_adresses_privees():
    # le Pi est une passerelle vers Internet, pas vers le réseau local
    interdites = [
        "http://127.0.0.1/admin",          # loopback
        "http://localhost:11434/api",      # notre propre Ollama !
        "http://192.168.1.1/",             # box / routeur
        "http://10.0.0.5/",                # LAN privé
        "http://169.254.169.254/",         # lien-local (métadonnées cloud)
        "http://[::1]/",                   # loopback IPv6
    ]
    for url in interdites:
        with pytest.raises(ValueError):
            commands._assert_public_host(url)


def test_web_accepte_une_adresse_publique():
    commands._assert_public_host("http://1.1.1.1/")   # ne doit pas lever


def test_web_refuse_les_autres_schemas():
    for url in ["ftp://exemple.ch/fichier", "file:///etc/passwd", "httpx://x"]:
        with pytest.raises(ValueError):
            commands._fetch_page(url)


def test_web_sans_argument_affiche_l_usage():
    reponse = commands.cmd_web("", "auto")
    assert "Usage" in reponse.text


def test_help_liste_toutes_les_commandes():
    texte = commands.cmd_help("", "auto").text
    for nom in commands.COMMANDS:
        assert f"/{nom}" in texte


# ---------------------------------------------------------------- /ask
def test_ask_sans_ia_ne_renvoie_pas_la_question(monkeypatch):
    # sans IA, compress() renvoie None et la commande doit répondre "IA indisponible"
    monkeypatch.setattr(commands, "compress", lambda c, i, m: None)
    reponse = commands.cmd_ask("capitale du Japon", "auto")
    assert "capitale du Japon" not in reponse.text
    assert "IA" in reponse.text


# ---------------------------------------------------------------- /meteo
def test_meteo_encode_la_ville_dans_l_url(monkeypatch):
    # une ville contenant "?", "#" ou "&" ne doit pas détourner l'URL wttr.in
    urls = []

    class FauxResp:
        text = "Geneve: ☀️ +25°C"
        def raise_for_status(self):
            pass

    monkeypatch.setattr(commands.requests, "get",
                        lambda url, timeout: urls.append(url) or FauxResp())
    commands.cmd_meteo("Le Locle?format=j1", "auto")
    assert "?format=%l" in urls[0]                 # le format prévu est intact
    assert "Le%20Locle%3Fformat%3Dj1" in urls[0]   # l'argument est encodé
