# app_pricer_cors.py
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, Response
from pydantic import BaseModel
from typing import Literal, Optional, Callable, Any
import importlib

import csv, os
from datetime import datetime, timezone

app = FastAPI(title="Simulateur Pricer")

# ⚠️ Mets bien ton domaine Netlify ici
ALLOWED_ORIGINS = [
    "https://simulateur-price.netlify.app",
    "http://localhost:8000",
    "http://127.0.0.1:8000",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------- Bridge vers pricer_engine ----------------
def _resolve_engine() -> Callable[..., dict]:
    """
    Essaie de trouver une fonction de calcul dans pricer_engine, quel que soit
    son nom (compute_annuity, compute, run, main, etc.) ou une méthode d'une classe.
    Retourne un callable (amount, currency, years, include_retro, extra_contract_fee) -> dict
    """
    pe = importlib.import_module("pricer_engine")

    # 1) Fonctions candidates directes
    candidates = [
        "compute_annuity",
        "compute",
        "compute_annuities",
        "run",
        "main",
        "price",
        "calculate",
    ]
    for name in candidates:
        fn = getattr(pe, name, None)
        if callable(fn):
            return fn

    # 2) Classe candidate avec méthode
    class_candidates = ["Engine", "PricerEngine", "Pricer", "Calculator"]
    method_candidates = ["compute_annuity", "compute", "run", "main", "calculate"]
    for cls_name in class_candidates:
        cls = getattr(pe, cls_name, None)
        if cls is not None:
            obj = cls() if callable(cls) else None
            if obj:
                for m in method_candidates:
                    fn = getattr(obj, m, None)
                    if callable(fn):
                        # Wrap pour avoir la même signature
                        def bound(*args, __fn=fn, **kwargs):
                            return __fn(*args, **kwargs)
                        return bound

    # 3) Rien trouvé : message explicite
    raise ImportError(
        "Impossible de trouver une fonction de calcul dans pricer_engine. "
        "Attendu: compute_annuity(amount, currency, years, include_retro, extra_contract_fee). "
        "Solutions: (a) renommer ta fonction en 'compute_annuity', ou "
        "(b) expose une des fonctions: compute, run, main, price, calculate, "
        "ou (c) une classe Engine/PricerEngine/Pricer/Calculator avec une méthode compute_annuity/compute/run/main."
    )

# On résout au démarrage
_COMPUTE_FN = _resolve_engine()

# ---------------- Modèles ----------------
class ComputeRequest(BaseModel):
    montant_disponible: float
    devise: Literal["EUR", "USD"]
    duree: int
    retrocessions: Literal["oui", "non"]
    frais_contrat: float = 0.0  # décimal, ex 0.0025 = 0,25%

class ComputeResponse(BaseModel):
    rente_annuelle_arrondie: float
    gestion_rate: float
    retro_rate: float
    garde_rate: float
    frais_contrat: float
    total_frais: float

class TrackEvent(BaseModel):
    event: Literal["pageview", "calculate_click", "calculate_success", "calculate_error"]
    montant: Optional[float] = None
    devise: Optional[Literal["EUR", "USD"]] = None
    duree: Optional[int] = None
    retro: Optional[Literal["oui", "non"]] = None
    support: Optional[Literal["assurance-vie", "compte-titres"]] = None
    frais_contrat: Optional[float] = None
    rente: Optional[float] = None
    error: Optional[str] = None

# ---------------- Tracking CSV (option 5) ----------------
CSV_PATH = "events_log.csv"
CSV_FIELDS = ["ts_utc","ip","ua","event","montant","devise","duree","retro","support","frais_contrat","rente","error"]

def append_event(row: dict):
    file_exists = os.path.exists(CSV_PATH)
    with open(CSV_PATH, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if not file_exists:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in CSV_FIELDS})

# ---------------- Routes confort ----------------
@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/docs")

@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return Response(status_code=204)

@app.get("/health")
def health():
    return {"status": "ok"}

# ---------------- Endpoint de calcul ----------------
@app.post("/compute", response_model=ComputeResponse)
def compute(req: ComputeRequest):
    try:
        # On appelle la fonction résolue, en conservant la signature attendue
        res = _COMPUTE_FN(
            amount=req.montant_disponible,
            currency=req.devise,
            years=req.duree,
            include_retro=(req.retrocessions == "oui"),
            extra_contract_fee=req.frais_contrat,
        )
    except TypeError:
        # Si la signature diffère (ex: ordre ou noms), on tente une adaptation
        # Stratégie: passer via **kwargs communs
        kwargs = {
            "amount": req.montant_disponible,
            "currency": req.devise,
            "years": req.duree,
            "include_retro": (req.retrocessions == "oui"),
            "extra_contract_fee": req.frais_contrat,
        }
        try:
            res = _COMPUTE_FN(**kwargs)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Appel moteur invalide: {e}")

    if not isinstance(res, dict):
        raise HTTPException(status_code=500, detail="Le moteur doit renvoyer un dict.")

    # Contrôle minimal des clés attendues
    expected = ["rente_annuelle_arrondie","gestion_rate","retro_rate","garde_rate","frais_contrat","total_frais"]
    for k in expected:
        if k not in res:
            raise HTTPException(status_code=500, detail=f"Clé manquante dans la réponse moteur: {k}")

    return res

# ---------------- Endpoint de tracking ----------------
@app.post("/track")
async def track(event: TrackEvent, request: Request):
    ip = request.client.host if request.client else "-"
    ua = request.headers.get("user-agent", "-")
    row = {
        "ts_utc": datetime.now(timezone.utc).isoformat(),
        "ip": ip,
        "ua": ua[:300],
        "event": event.event,
        "montant": event.montant,
        "devise": event.devise,
        "duree": event.duree,
        "retro": event.retro,
        "support": event.support,
        "frais_contrat": event.frais_contrat,
        "rente": event.rente,
        "error": (event.error or "")[:300],
    }
    append_event(row)
    return {"ok": True}
