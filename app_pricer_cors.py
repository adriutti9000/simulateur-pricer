# app_pricer_cors.py
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, Response
from pydantic import BaseModel
from typing import Literal, Optional
from pricer_engine import compute_annuity  # <-- ton moteur existant

import csv
import os
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

# ---------- Modèles I/O ----------
class ComputeRequest(BaseModel):
    montant_disponible: float
    devise: Literal["EUR", "USD"]
    duree: int
    retrocessions: Literal["oui", "non"]
    frais_contrat: float = 0.0  # assurance-vie en décimal (ex. 0.0025 = 0,25%)

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


# ---------- Utils logging CSV ----------
CSV_PATH = "events_log.csv"
CSV_FIELDS = [
    "ts_utc",
    "ip",
    "ua",
    "event",
    "montant",
    "devise",
    "duree",
    "retro",
    "support",
    "frais_contrat",
    "rente",
    "error",
]

def append_event(row: dict):
    """Append une ligne dans events_log.csv (crée l'entête si nouveau fichier)."""
    file_exists = os.path.exists(CSV_PATH)
    with open(CSV_PATH, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if not file_exists:
            writer.writeheader()
        writer.writerow({k: row.get(k, "") for k in CSV_FIELDS})


# ---------- Routes confort ----------
@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/docs")

@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return Response(status_code=204)

@app.get("/health")
def health():
    return {"status": "ok"}


# ---------- Compute ----------
@app.post("/compute", response_model=ComputeResponse)
def compute(req: ComputeRequest):
    res = compute_annuity(
        amount=req.montant_disponible,
        currency=req.devise,
        years=req.duree,
        include_retro=(req.retrocessions == "oui"),
        extra_contract_fee=req.frais_contrat,
    )
    return res


# ---------- Tracking ----------
@app.post("/track")
async def track(event: TrackEvent, request: Request):
    # Métadonnées requête
    ip = request.client.host if request.client else "-"
    ua = request.headers.get("user-agent", "-")

    # Ligne CSV
    row = {
        "ts_utc": datetime.now(timezone.utc).isoformat(),
        "ip": ip,
        "ua": ua[:300],  # évite lignes trop longues
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
