from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, field_validator
from typing import Optional
from pricer_engine import Inputs as EngineInputs, compute as engine_compute

app = FastAPI(title="Pricer API", version="1.1.0", description="API HTTP pour le moteur de rente")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # en prod: restreindre
    allow_methods=["*"],
    allow_headers=["*"],
)

class ComputeRequest(BaseModel):
    montant_disponible: float
    devise: str
    duree: int
    retrocessions: bool | str
    retro_rate: Optional[float] = None
    gestion_rate: Optional[float] = None
    garde_rate: Optional[float] = 0.001

    @field_validator("devise")
    @classmethod
    def _devise_upper(cls, v: str):
        v = v.upper()
        if v not in ("EUR", "USD"):
            raise ValueError("devise doit être EUR ou USD")
        return v

    @field_validator("duree")
    @classmethod
    def _duree_bounds(cls, v: int):
        if not (1 <= v <= 15):
            raise ValueError("duree doit être entre 1 et 15 ans")
        return v

    @field_validator("retrocessions")
    @classmethod
    def _retro_bool(cls, v):
        if isinstance(v, bool): return v
        s = str(v).strip().lower()
        if s in {"oui","o","yes","y","true","1"}: return True
        if s in {"non","n","no","false","0"}: return False
        raise ValueError("retrocessions doit être booléen ou 'oui'/'non'")

class ComputeResponse(BaseModel):
    rente_annuelle_arrondie: float
    rente_annuelle_brut: float
    total_frais: float
    taux_direct: float
    taux_moyen_pondere: float
    retro_rate: float
    gestion_rate: float
    garde_rate: float

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/compute", response_model=ComputeResponse)
def compute(req: ComputeRequest):
    try:
        out = engine_compute(EngineInputs(
            montant_disponible=req.montant_disponible,
            devise=req.devise,
            duree=req.duree,
            retrocessions=req.retrocessions,
            retro_rate=req.retro_rate,
            gestion_rate=req.gestion_rate,
            garde_rate=req.garde_rate
        ))
        return ComputeResponse(
            rente_annuelle_arrondie=out.rente_annuelle_arrondie,
            rente_annuelle_brut=out.rente_annuelle_brut,
            total_frais=out.total_frais,
            taux_direct=out.taux_direct,
            taux_moyen_pondere=out.taux_moyen_pondere,
            retro_rate=out.retro_rate,
            gestion_rate=out.gestion_rate,
            garde_rate=out.garde_rate
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
