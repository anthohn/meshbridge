"""
MeshBridge — Instrumentation : compteurs en mémoire + journal CSV.
Chaque requête traitée ajoute une ligne dans metrics.csv (racine du repo,
ignoré par Git) et met à jour les compteurs que /stats résume.
Les chiffres servent aussi au rapport : taux de compression, latences,
répartition cloud/local/brut.
"""
import csv
import time
import logging
from pathlib import Path

log = logging.getLogger("meshbridge.metrics")

CSV_PATH = Path(__file__).resolve().parent.parent / "metrics.csv"
CSV_HEADER = ["horodatage", "commande", "mode", "source",
              "octets_entree", "octets_sortie", "latence_s"]

# État depuis le démarrage du bridge (remis à zéro à chaque relance)
_start = time.time()
_nb_total = 0
_latence_totale = 0.0
_par_source = {}          # ex. {"cloud": 12, "local": 3, "-": 5}


def record(verb: str, mode: str, source: str | None,
           in_len: int, out_len: int, dt: float) -> None:
    """Enregistre une requête traitée. Ne doit jamais faire planter le bridge."""
    global _nb_total, _latence_totale
    _nb_total += 1
    _latence_totale += dt
    cle = source or "-"                      # "-" = pas d'IA (ping, help…)
    _par_source[cle] = _par_source.get(cle, 0) + 1

    try:
        nouveau = not CSV_PATH.exists()
        with open(CSV_PATH, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if nouveau:
                w.writerow(CSV_HEADER)
            w.writerow([time.strftime("%Y-%m-%d %H:%M:%S"), verb, mode, cle,
                        in_len, out_len, f"{dt:.1f}"])
    except OSError as e:
        log.warning(f"écriture de {CSV_PATH.name} impossible : {e}")


def summary() -> str:
    """Résumé compact de l'état du relai, taillé pour un message LoRa."""
    secondes = int(time.time() - _start)
    jours, reste = divmod(secondes, 86400)
    heures, reste = divmod(reste, 3600)
    minutes = reste // 60
    uptime = f"{jours}j{heures}h" if jours else f"{heures}h{minutes:02d}"

    if _nb_total == 0:
        return f"📊 up {uptime} · aucune requête traitée"

    lat_moy = _latence_totale / _nb_total
    sources = " ".join(f"{k}:{v}" for k, v in sorted(_par_source.items()))
    return f"📊 up {uptime} · {_nb_total} req · {sources} · lat moy {lat_moy:.1f}s"
