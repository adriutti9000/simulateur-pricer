# pricer_engine.py
from typing import Dict

# Courbes de taux actualisées
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


def compute_annuity(
    amount: float,
    currency: str,
    years: int,
    include_retro: bool,
    extra_contract_fee: float = 0.0,
) -> Dict[str, float]:
    """Renvoie la rente annuelle nette et les frais associés."""

    # --- Vérifications basiques ---
    if currency not in _CURVE:
        raise ValueError(f"Devise non supportée : {currency}")
    if years not in _CURVE[currency]:
        raise ValueError(f"Durée non disponible : {years} ans")

    # --- Récupération du taux ---
    rate = _CURVE[currency][years] / 100  # en décimal

    # --- Calcul des frais selon le barème ---
    # 1. Frais de rétrocession
    if amount < 10_000_000:
        retro_rate = 0.0021
    elif amount < 15_000_000:
        retro_rate = 0.0018
    else:
        retro_rate = 0.0015

    # 2. Frais de gestion (dépendent du rétro)
    if include_retro:
        if amount < 10_000_000:
            gestion_rate = 0.0049
        elif amount < 15_000_000:
            gestion_rate = 0.0042
        else:
            gestion_rate = 0.0035
    else:
        if amount < 10_000_000:
            gestion_rate = 0.0060
        elif amount < 15_000_000:
            gestion_rate = 0.0050
        else:
            gestion_rate = 0.0040
        retro_rate = 0.0  # pas de rétro

    # 3. Droit de garde (fixe)
    garde_rate = 0.0005  # 0.05 %

    # 4. Frais de contrat (si assurance-vie)
    frais_contrat = extra_contract_fee or 0.0

    # --- Total des frais ---
    total_frais = gestion_rate + garde_rate + frais_contrat
    # Les rétro sont incluses dans gestion_rate quand applicable

    # --- Calcul rente nette annuelle ---
    rente_nette = amount * rate * (1 - total_frais)
    rente_arrondie = round(rente_nette, 2)

    return {
        "rente_annuelle_arrondie": rente_arrondie,
        "gestion_rate": gestion_rate,
        "retro_rate": retro_rate,
        "garde_rate": garde_rate,
        "frais_contrat": frais_contrat,
        "total_frais": total_frais,
    }
