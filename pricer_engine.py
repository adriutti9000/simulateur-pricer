# pricer_engine.py
from typing import Dict

# Courbes de taux (inchangées)
_TENORS = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15]
_CURVE: Dict[str, Dict[int, float]] = {
    "EUR": {
        1: 2.336, 2: 2.457, 3: 2.627, 4: 2.823, 5: 3.012,
        6: 3.1735, 7: 3.335, 8: 3.463, 9: 3.577, 10: 3.692,
        11: 3.8028, 12: 3.9136, 13: 4.0244, 14: 4.1352, 15: 4.246,
    },
    "USD": {
        1: 4.215, 2: 4.100, 3: 4.127, 4: 4.238, 5: 4.382,
        6: 4.5470, 7: 4.717, 8: 4.873, 9: 5.010, 10: 5.132,
        11: 5.2410, 12: 5.3500, 13: 5.426667, 14: 5.503333, 15: 5.580,
    },
}

# Barème rétrocessions (en décimal)
def _retro_rate(amount: float) -> float:
    if amount < 10_000_000:
        return 0.0021  # 0,21 %
    elif amount < 15_000_000:
        return 0.0018  # 0,18 %
    else:
        return 0.0015  # 0,15 %

# Barème frais de gestion quand rétrocessions = Oui  (déjà "tout compris" côté gestion)
def _gestion_rate_with_retro(amount: float) -> float:
    if amount < 10_000_000:
        return 0.0049  # 0,49 %
    elif amount < 15_000_000:
        return 0.0042  # 0,42 %
    else:
        return 0.0035  # 0,35 %

# Barème frais de gestion quand rétrocessions = Non
def _gestion_rate_without_retro(amount: float) -> float:
    if amount < 10_000_000:
        return 0.0060  # 0,60 %
    elif amount < 15_000_000:
        return 0.0050  # 0,50 %
    else:
        return 0.0040  # 0,40 %

def compute_annuity(
    amount: float,
    currency: str,
    years: int,
    include_retro: bool,
    extra_contract_fee: float = 0.0,  # ex: 0.001 = 0,10 %
) -> Dict[str, float]:
    """
    Retourne :
      - rente_annuelle_arrondie (entier, sans décimales)
      - gestion_rate, retro_rate, garde_rate, frais_contrat, total_frais (décimaux)
    Rente nette utilisée côté front : amount * curve_rate * (1 - total_frais)
    """
    if currency not in _CURVE:
        raise ValueError(f"Devise non supportée : {currency}")
    if years not in _CURVE[currency]:
        raise ValueError(f"Durée non disponible : {years} ans")

    # Taux "marché" de la courbe (en décimal)
    curve_rate = _CURVE[currency][years] / 100.0

    # Frais de gestion & rétro (affichage)
    if include_retro:
        gestion_rate = _gestion_rate_with_retro(amount)
        retro_rate = _retro_rate(amount)   # affiché à part, mais DEJA inclus dans gestion_rate
    else:
        gestion_rate = _gestion_rate_without_retro(amount)
        retro_rate = 0.0

    # Droits de garde = 0,10 %
    garde_rate = 0.0010

    # Frais d’assurance-vie (si support = assurance-vie)
    contract_rate = max(0.0, float(extra_contract_fee or 0.0))

    # >>> TOTAL DES FRAIS SANS AUCUNE SOUSTRACTION DE LA RÉTRO <<<
    total_frais = gestion_rate + garde_rate + contract_rate

    # Rente nette annuelle (pas de décimales)
    rente_nette = amount * curve_rate * (1.0 - total_frais)
    rente_arrondie = int(round(rente_nette))

    return {
        "rente_annuelle_arrondie": rente_arrondie,
        "gestion_rate": round(gestion_rate, 6),
        "retro_rate": round(retro_rate, 6),
        "garde_rate": round(garde_rate, 6),
        "frais_contrat": round(contract_rate, 6),
        "total_frais": round(total_frais, 6),
    }
