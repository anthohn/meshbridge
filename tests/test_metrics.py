"""Tests des compteurs /stats et du journal metrics.csv."""
import config
import metrics


def test_summary_sans_aucune_requete():
    assert "aucune requête" in metrics.summary()


def test_record_ecrit_une_ligne_csv():
    metrics.record("web", "auto", "cloud", 48000, 187, 6.4)

    lignes = metrics.CSV_PATH.read_text(encoding="utf-8").strip().splitlines()
    assert len(lignes) == 2                    # entête + 1 requête
    assert lignes[0].split(",") == metrics.CSV_HEADER
    champs = lignes[1].split(",")
    assert champs[1:] == ["web", "auto", "cloud", "48000", "187", "6.4"]


def test_summary_compte_les_requetes_par_source():
    metrics.record("ask", "auto", "cloud", 10, 50, 2.0)
    metrics.record("web", "auto", "cloud", 500, 60, 4.0)
    metrics.record("ask", "local", "local", 10, 40, 12.0)

    resume = metrics.summary()
    assert "3 req" in resume
    assert "cloud:2" in resume
    assert "local:1" in resume
    assert "lat moy 6.0s" in resume            # (2 + 4 + 12) / 3


def test_summary_tient_dans_un_paquet_lora():
    for i in range(50):
        metrics.record("ask", "auto", "cloud", 100, 50, 3.0)
    assert len(metrics.summary().encode("utf-8")) <= config.MAX_LEN


def test_record_survit_a_un_csv_inaccessible(monkeypatch, tmp_path):
    # les stats ne doivent jamais faire tomber le relai
    monkeypatch.setattr(metrics, "CSV_PATH", tmp_path / "dossier-inexistant" / "m.csv")
    metrics.record("ping", "auto", None, 0, 20, 0.1)   # ne doit pas lever
    assert "1 req" in metrics.summary()                # compté quand même
