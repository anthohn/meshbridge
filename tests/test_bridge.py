"""Tests du filtre d'authentification de on_receive : le bridge ne doit
obéir qu'aux messages directs chiffrés signés par Paul. On importe le vrai
bridge ; seuls l'interface radio et le paquet reçu sont simulés."""
import pytest

import config
import bridge

PIERRE = 0x1111        # notre nœud (destinataire des DM)
PAUL   = 0x2222        # le nœud autorisé
CLE_PAUL = "cle-publique-de-paul"


class FausseInterface:
    """Interface minimale : expose notre numéro, la nodedb, et note les envois."""
    def __init__(self, cle_vue=CLE_PAUL):
        self.myInfo = type("MyInfo", (), {"my_node_num": PIERRE})
        self.nodesByNum = {PAUL: {"user": {"publicKey": cle_vue}}}
        self.emis = []

    def sendText(self, text, destinationId=None, pkiEncrypted=False):
        self.emis.append((text, destinationId))


def paquet(texte, source=PAUL, dest=PIERRE, pki=True):
    p = {"from": source, "to": dest,
         "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": texte}}
    if pki:
        p["pkiEncrypted"] = True     # MessageToDict n'inclut le champ que s'il est vrai
    return p


def vider_la_file():
    items = []
    while not bridge._tasks.empty():
        items.append(bridge._tasks.get_nowait())
    return items


@pytest.fixture(autouse=True)
def contexte(monkeypatch):
    """Paul autorisé, clé épinglée, interface branchée, file propre."""
    monkeypatch.setattr(config, "PAUL_NODE_NUM", PAUL)
    monkeypatch.setattr(config, "PAUL_PUBLIC_KEY", CLE_PAUL)
    iface = FausseInterface()
    monkeypatch.setitem(bridge._iface, "handle", iface)
    vider_la_file()
    yield iface
    vider_la_file()


def test_dm_signe_de_paul_est_enfile(contexte):
    bridge.on_receive(paquet("/ping"), contexte)
    assert vider_la_file() == [(PAUL, "ping", "")]


def test_broadcast_est_ignore(contexte):
    # message non adressé à Pierre (diffusion) → refusé
    bridge.on_receive(paquet("/ping", dest=0xFFFFFFFF), contexte)
    assert vider_la_file() == []


def test_dm_non_chiffre_est_ignore(contexte):
    bridge.on_receive(paquet("/ping", pki=False), contexte)
    assert vider_la_file() == []


def test_dm_d_un_autre_noeud_est_ignore(contexte):
    bridge.on_receive(paquet("/ping", source=0x9999), contexte)
    assert vider_la_file() == []


def test_cle_publique_qui_ne_correspond_pas_est_ignoree(monkeypatch):
    # nodedb empoisonnée : Pierre voit une clé différente de celle épinglée
    monkeypatch.setattr(config, "PAUL_NODE_NUM", PAUL)
    monkeypatch.setattr(config, "PAUL_PUBLIC_KEY", CLE_PAUL)
    iface = FausseInterface(cle_vue="cle-de-l-attaquant")
    monkeypatch.setitem(bridge._iface, "handle", iface)
    bridge.on_receive(paquet("/ping"), iface)
    assert vider_la_file() == []


def test_message_sans_slash_est_ignore(contexte):
    bridge.on_receive(paquet("bonjour Pierre"), contexte)
    assert vider_la_file() == []
