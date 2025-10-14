# pricer_engine.py — moteur autonome (avec détail des frais)
from dataclasses import dataclass
from typing import Optional, Dict
import math
import argparse

_TENORS = [1,2,3,4,5,6,7,8,9,10,11,12,13,14,15]
_CURVE: Dict[str, Dict[int, float]] = {
    "EUR": {
        1: 2.325,  2: 2.47,  3: 2.651,  4: 2.851,  5: 3.038,
        6: 3.15, 7: 3.337,  8: 3.453,  9: 3.554, 10: 3.659,
        11: 3.77,12: 3.88,13: 3.99,14: 4.09,15: 4.20
    },
    "USD": {
        1: 4.20,  2: 4.13,  3: 4.188,  4: 4.313,   5: 4.457,
        6: 4.61, 7: 4.765,  8: 4.906,  9: 5.032,  10: 5.144,
        11: 5.23,12: 5.31,13: 5.39,14: 5.47,15: 5.555
    }
}

@dataclass
class Inputs:
    montant_disponible: float
    devise: str
    duree: int
    retrocessions: bool
    retro_rate: Optional[float] = None
    gestion_rate: Optional[float] = None
    garde_rate: Optional[float] = 0.001  # 0,10%

@dataclass
class Outputs:
    rente_annuelle_arrondie: float
    rente_annuelle_brut: float
    total_frais: float
    taux_direct: float
    taux_moyen_pondere: float
    retro_rate: float
    gestion_rate: float
    garde_rate: float

def _rate_from_curve(devise: str, duree: int) -> float:
    return _CURVE[devise][int(duree)]

def _avg_curve_adjusted(devise: str, duree: int) -> float:
    series = [_CURVE[devise][k] for k in range(1, duree+1)]
    avg = sum(series)/len(series)
    return avg * ((duree + 1) / (duree * 2))

def _retro_tier(montant: float, retro_on: bool) -> float:
    """
    Frais de rétro (si rétro ON) :
      - < 10 000 000         -> 0,21 %
      - 10 000 000 à < 15 M  -> 0,18 %
      - >= 15 000 000        -> 0,15 %
    Sinon (rétro OFF) -> 0,00 %
    """
    if not retro_on:
        return 0.0
    if montant < 10_000_000:
        return 0.0021
    if montant < 15_000_000:          # implique 10 000 000 <= montant < 15 000 000
        return 0.0018
    return 0.0015                      # montant >= 15 000 000


def _gestion_tier(montant: float, retro_on: bool) -> float:
    """
    Frais de gestion :
      Si rétro ON :
        - < 10 000 000         -> 0,49 %
        - 10 000 000 à < 15 M  -> 0,42 %
        - >= 15 000 000        -> 0,35 %
      Si rétro OFF :
        - < 10 000 000         -> 0,60 %
        - 10 000 000 à < 15 M  -> 0,50 %
        - >= 15 000 000        -> 0,40 %
    """
    if retro_on:
        if montant < 10_000_000:
            return 0.0049
        if montant < 15_000_000:
            return 0.0042
        return 0.0035
    else:
        if montant < 10_000_000:
            return 0.0060
        if montant < 15_000_000:
            return 0.0050
        return 0.0040

def compute(x: Inputs) -> Outputs:
    retro = _retro_tier(x.montant_disponible, x.retrocessions) if x.retro_rate is None else x.retro_rate
    gestion = _gestion_tier(x.montant_disponible, x.retrocessions) if x.gestion_rate is None else x.gestion_rate
    garde = x.garde_rate if x.garde_rate is not None else 0.0
    total_frais = retro + gestion + garde

    r_direct = _rate_from_curve(x.devise, x.duree)
    factor = (1 + ((r_direct/100.0) - total_frais)) ** x.duree
    pv_equiv = x.montant_disponible / factor
    diff = x.montant_disponible - pv_equiv
    r_avg_adj = _avg_curve_adjusted(x.devise, x.duree)
    acc = diff * (1 + (r_avg_adj/100.0)) ** x.duree
    rente_brut = acc / x.duree
    rente_arr = math.ceil(rente_brut / 1000.0) * 1000.0

    return Outputs(
        rente_annuelle_arrondie=rente_arr,
        rente_annuelle_brut=rente_brut,
        total_frais=total_frais,
        taux_direct=r_direct,
        taux_moyen_pondere=r_avg_adj,
        retro_rate=retro,
        gestion_rate=gestion,
        garde_rate=garde
    )

# CLI facultative
def _parse_bool_oui_non(s: str) -> bool:
    s = s.strip().lower()
    if s in {"oui","o","yes","y","true","1"}: return True
    if s in {"non","n","no","false","0"}: return False
    raise argparse.ArgumentTypeError("Utilise 'oui' ou 'non'")

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--montant", type=float, required=True)
    p.add_argument("--devise", type=str, required=True, choices=list(_CURVE.keys()))
    p.add_argument("--duree", type=int, required=True, choices=_TENORS)
    p.add_argument("--retro", type=_parse_bool_oui_non, default="oui")
    args = p.parse_args()
    out = compute(Inputs(args.montant, args.devise, args.duree, args.retro))
    print(out)
