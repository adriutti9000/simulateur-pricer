# pricer_engine.py
from typing import Dict

# Courbes de taux (inchangées)
_TENORS = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15]
_CURVE: Dict[str, Dict[int, float]] = {
    "EUR": {
        1: 2.4400, 2: 2.6300, 3: 2.8500, 4: 3.0600, 5: 3.2600,
        6: 3.4100, 7: 3.5200, 8: 3.6600, 9: 3.7500, 10: 3.8500,
        11: 3.9700, 12: 4.0900, 13: 4.2100, 14: 4.3300, 15: 4.4500,
    },
    "USD": {
        1: 4.5983, 2: 4.7804, 3: 4.9351, 4: 5.0762, 5: 5.1764,
        6: 5.3038, 7: 5.4176, 8: 5.5132, 9: 5.6088, 10: 5.6953,
        11: 5.7817, 12: 5.8227, 13: 5.8864, 14: 5.9502, 15: 6.0139,
    },
}

# --- Barèmes ---

def _retro_rate(amount: float) -> float:
    """Rétrocessions (décimal)"""
    if amount < 10_000_000:
        return 0.0021  # 0,21 %
    elif amount < 15_000_000:
        return 0.0018  # 0,18 %
    else:
        return 0.0015  # 0,15 %

def _gestion_with_retro_base(amount: float) -> float:
    """Barème gestion quand rétro = Oui (hors rétro elle-même) : 0,49 / 0,42 / 0,35."""
    if amount < 10_000_000:
        return 0.0049  # 0,49 %
    elif amount < 15_000_000:
        return 0.0042  # 0,42 %
    else:
        return 0.0035  # 0,35 %

def _gestion_without_retro(amount: float) -> float:
    """Barème gestion quand rétro = Non : 0,60 / 0,50 / 0,40."""
    if amount < 10_000_000:
        return 0.0060  # 0,60 %
    elif amount < 15_000_000:
        return 0.0050  # 0,50 %
    else:
        return 0.0040  # 0,40 %

_GARDE = 0.0010  # 0,10 %

# --- Moteur ---

def compute_annuity(
    amount: float,
    currency: str,
    years: int,
    include_retro: bool,
    extra_contract_fee: float = 0.0,  # ex: 0.001 = 0,10 %
) -> Dict[str, float]:
    """
    Retourne un dict avec :
      - rente_annuelle_arrondie (entier, sans décimales)
      - gestion_rate (valeur affichée), retro_rate (info), garde_rate, frais_contrat, total_frais
    Rente nette = montant * taux_courbe * (1 - total_frais)
    """
    if currency not in _CURVE:
        raise ValueError(f"Devise non supportée : {currency}")
    if years not in _CURVE[currency]:
        raise ValueError(f"Durée non disponible : {years} ans")

    curve_rate = _CURVE[currency][years] / 100.0  # décimal

    if include_retro:
        # Gestion affichée = barème gestion (avec rétro) + rétro (affichée séparément)
        gestion_base = _gestion_with_retro_base(amount)  # ex: 0,0035
        retro_rate = _retro_rate(amount)                 # ex: 0,0015
        gestion_display = gestion_base + retro_rate      # ex: 0,0035 + 0,0015 = 0,0050 (0,50 %)
    else:
        gestion_display = _gestion_without_retro(amount) # ex: 0,0040 / 0,0050 / 0,0060
        retro_rate = 0.0

    garde_rate = _GARDE
    contract_rate = max(0.0, float(extra_contract_fee or 0.0))

    # TOTAL = gestion (affichée) + garde + contrat
    total_frais = gestion_display + garde_rate + contract_rate

    rente_nette = amount * curve_rate * (1.0 - total_frais)
    rente_arrondie = int(round(rente_nette))  # sans décimales

    return {
        "rente_annuelle_arrondie": rente_arrondie,
        "gestion_rate": round(gestion_display, 6),  # valeur à AFFICHER
        "retro_rate": round(retro_rate, 6),         # info pour la note
        "garde_rate": round(garde_rate, 6),
        "frais_contrat": round(contract_rate, 6),
        "total_frais": round(total_frais, 6),
    }
