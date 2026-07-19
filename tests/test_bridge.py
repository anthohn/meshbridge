"""Tests du filtre d'authentification de on_receive : le bridge ne doit obéir
qu'aux messages directs chiffrés (PKC) venant de Paul. On importe le vrai
bridge ; seuls l'interface radio et le paquet reçu sont simulés."""
import pytest

import config
import bridge

PAUL = 0x2222        # le nœud autorisé


class FausseInterface:
    """Interface minimale : note les envois (pour vérifier qu'on ne répond pas)."""
    def __init__(self):
        self.emis = []

    def sendText(self, text, destinationId=None, pkiEncrypted=False):
        self.emis.append((text, destinationId))


def paquet(texte, source=PAUL, chiffre=True):
    p = {"from": source, "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": texte}}
    if chiffre:
        p["pkiEncrypted"] = True     # --info n'inclut le champ que s'il est vrai
    return p


def vider_la_file():
    items = []
    while not bridge._tasks.empty():
        items.append(bridge._tasks.get_nowait())
    return items


@pytest.fixture(autouse=True)
def contexte(monkeypatch):
    """Paul autorisé, interface branchée, file propre."""
    monkeypatch.setattr(config, "PAUL_NODE_NUM", PAUL)
    iface = FausseInterface()
    monkeypatch.setitem(bridge._iface, "handle", iface)
    vider_la_file()
    yield iface
    vider_la_file()


def test_dm_chiffre_de_paul_est_enfile(contexte):
    bridge.on_receive(paquet("/ping"), contexte)
    assert vider_la_file() == [(PAUL, "ping", "")]


def test_dm_non_chiffre_est_ignore(contexte):
    bridge.on_receive(paquet("/ping", chiffre=False), contexte)
    assert vider_la_file() == []


def test_dm_d_un_autre_noeud_est_ignore(contexte):
    bridge.on_receive(paquet("/ping", source=0x9999), contexte)
    assert vider_la_file() == []


def test_message_sans_slash_est_ignore(contexte):
    bridge.on_receive(paquet("bonjour Pierre"), contexte)
    assert vider_la_file() == []
