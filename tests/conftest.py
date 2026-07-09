"""
Préparation commune des tests.
- rend src/ importable (le projet n'est pas un package installé) ;
- fournit un faux module google.genai si la vraie lib n'est pas installée
  (elle ne l'est que sur le Pi ; les tests mockent tous les appels réseau) ;
- isole les métriques : CSV jetable + compteurs remis à zéro à chaque test.
"""
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

try:
    from google import genai            # noqa: F401
except ImportError:
    google = types.ModuleType("google")
    google.genai = types.ModuleType("google.genai")
    sys.modules["google"] = google
    sys.modules["google.genai"] = google.genai

import pytest
import metrics


@pytest.fixture(autouse=True)
def metriques_isolees(tmp_path, monkeypatch):
    """Chaque test repart avec des compteurs vides et un CSV temporaire."""
    monkeypatch.setattr(metrics, "CSV_PATH", tmp_path / "metrics.csv")
    monkeypatch.setattr(metrics, "_nb_total", 0)
    monkeypatch.setattr(metrics, "_latence_totale", 0.0)
    monkeypatch.setattr(metrics, "_par_source", {})
