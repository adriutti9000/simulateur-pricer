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

def compute_annuity(
    amount: float,
    currency: str,
    years: int,
    include_retro: bool,
    extra_contract_fee: float = 0.0,
) -> Dict[str, float]:
    """Calcule la rente annuelle nette et les frais (même logique que ton fichier, avec corrections demandées)."""
    if currency not in _CURVE:
        raise ValueError(f"Devise non supportée : {currency}")
    if years not in _CURVE[currency]:
        raise ValueError(f"Durée non disponible : {years} ans")

    rate = _CURVE[currency][years] / 100.0  # taux en décimal

    # Barème rétrocessions (toujours calculé, mais pris en compte seulement si include_retro)
    if amount < 10_000_000:
        retro_rate = 0.0021  # 0,21 %
    elif amount < 15_000_000:
        retro_rate = 0.0018  # 0,18 %
    else:
        retro_rate = 0.0015  # 0,15 %

    # Barème frais de gestion (dépend de include_retro)
    if include_retro:
        if amount < 10_000_000:
            gestion_rate = 0.0049  # 0,49 %
        elif amount < 15_000_000:
            gestion_rate = 0.0042  # 0,42 %
        else:
            gestion_rate = 0.0035  # 0,35 %
        # rétro affichée séparément, mais INCLUSE dans la logique des frais de gestion (pas retranchée du total)
    else:
        if amount < 10_000_000:
            gestion_rate = 0.0060  # 0,60 %
        elif amount < 15_000_000:
            gestion_rate = 0.0050  # 0,50 %
        else:
            gestion_rate = 0.0040  # 0,40 %
        retro_rate = 0.0  # pas de rétro quand include_retro = False

    # ✅ Droits de garde = 0,10 %
    garde_rate = 0.0010

    # Frais d’assurance-vie (déjà en décimal, ex: 0.001 = 0,10 %)
    frais_contrat = float(extra_contract_fee or 0.0)

    # ✅ Total des frais : on n’enlève JAMAIS la rétro (elle est incluse dans gestion_rate si applicable)
    total_frais = gestion_rate + garde_rate + frais_contrat

    # Rente nette (arrondie sans décimales, comme demandé)
    rente_nette = amount * rate * (1 - total_frais)
    rente_arrondie = int(round(rente_nette))

    return {
        "rente_annuelle_arrondie": rente_arrondie,  # entier, sans décimales
        "gestion_rate": gestion_rate,
        "retro_rate": retro_rate,
        "garde_rate": garde_rate,
        "frais_contrat": frais_contrat,
        "total_frais": total_frais,
    }
