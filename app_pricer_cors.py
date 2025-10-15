# app_pricer_cors.py
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, Response, FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel
from typing import Literal, Optional, Dict, List
from datetime import datetime, timezone, date, timedelta
import csv, os

from pricer_engine import compute_annuity  # ❗ ne change pas

app = FastAPI(title="Simulateur Pricer")

# --- CORS : ton domaine Netlify + local ---
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

# ---------- Modèles ----------
class ComputeRequest(BaseModel):
    montant_disponible: float
    devise: Literal["EUR", "USD"]
    duree: int
    retrocessions: Literal["oui", "non"]
    frais_contrat: float = 0.0

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

# ---------- Tracking (CSV) ----------
CSV_PATH = "events_log.csv"
CSV_FIELDS = ["ts_utc","ip","ua","event","montant","devise","duree","retro","support","frais_contrat","rente","error"]

def append_event(row: dict):
    file_exists = os.path.exists(CSV_PATH)
    with open(CSV_PATH, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if not file_exists:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in CSV_FIELDS})

def load_events() -> List[Dict[str, str]]:
    if not os.path.exists(CSV_PATH):
        return []
    with open(CSV_PATH, "r", newline="", encoding="utf-8") as f:
        r = csv.DictReader(f)
        return list(r)

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

# ---------- Calcul ----------
@app.post("/compute", response_model=ComputeResponse)
def compute(req: ComputeRequest):
    try:
        return compute_annuity(
            amount=req.montant_disponible,
            currency=req.devise,
            years=req.duree,
            include_retro=(req.retrocessions == "oui"),
            extra_contract_fee=req.frais_contrat,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erreur moteur : {str(e)}")

# ---------- Tracking : reçoit les events du front ----------
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

# ---------- EXPORT : CSV brut ----------
@app.get("/events.csv", response_class=FileResponse)
def download_events_csv():
    if not os.path.exists(CSV_PATH):
        # retourne un CSV vide avec en-têtes
        with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
            w.writeheader()
    return FileResponse(CSV_PATH, media_type="text/csv", filename="events_log.csv")

# ---------- STATS : JSON agrégées ----------
@app.get("/stats", response_class=JSONResponse)
def stats(days: int = 30):
    days = max(1, min(days, 365))
    data = load_events()
    # Totaux simples
    total = len(data)
    by_event: Dict[str, int] = {}
    for row in data:
        by_event[row["event"]] = by_event.get(row["event"], 0) + 1

    # Série quotidienne (UTC) pour les X derniers jours
    end = date.today()
    start = end - timedelta(days=days - 1)
    # init structure
    per_day = { (start + timedelta(d)): {"pageview":0,"calculate_click":0,"calculate_success":0,"calculate_error":0}
                for d in range(days) }

    for row in data:
        try:
            ts = datetime.fromisoformat(row["ts_utc"].replace("Z","+00:00")).date()
        except Exception:
            continue
        if start <= ts <= end and row["event"] in per_day[ts]:
            per_day[ts][row["event"]] += 1

    # conversion JSON-friendly
    series = [
        {"date": d.isoformat(),
         **per_day[d]}
        for d in sorted(per_day.keys())
    ]

    return {
        "range_days": days,
        "total_events": total,
        "by_event": by_event,
        "series": series
    }

# ---------- Mini dashboard HTML ----------
@app.get("/stats.html", response_class=HTMLResponse)
def stats_html(days: int = 30):
    j = stats(days)
    rows = "".join(
        f"<tr><td>{r['date']}</td>"
        f"<td style='text-align:right'>{r['pageview']}</td>"
        f"<td style='text-align:right'>{r['calculate_click']}</td>"
        f"<td style='text-align:right'>{r['calculate_success']}</td>"
        f"<td style='text-align:right;color:{'#b91c1c' if r['calculate_error'] else '#0f172a'}'>{r['calculate_error']}</td>"
        f"</tr>"
        for r in j["series"]
    )
    html = f"""
    <html><head><meta charset="utf-8">
    <title>Stats - Simulateur Pricer</title>
    <style>
      body{{font-family:Arial,system-ui; color:#0f172a; background:#f8fafc; padding:24px}}
      .card{{max-width:900px;margin:0 auto;background:#fff;border-radius:12px;box-shadow:0 6px 16px rgba(0,0,0,.06);padding:20px}}
      h2{{margin:0 0 8px}}
      table{{width:100%; border-collapse:collapse; font-size:14px}}
      th,td{{padding:8px 10px; border-bottom:1px solid #e2e8f0}}
      th{{text-align:left; background:#f1f5f9}}
      .meta{{margin:10px 0 16px; color:#475569}}
      .pill{{display:inline-block;background:#eef2ff;color:#3730a3;padding:4px 8px;border-radius:999px;margin-right:6px}}
      .kpi{{display:inline-block;margin-right:14px}}
      .kpi b{{font-size:16px}}
    </style>
    </head>
    <body>
      <div class="card">
        <h2>Statistiques d'usage</h2>
        <div class="meta">
          Période: {j['range_days']} jours — 
          <span class="kpi">Total events: <b>{j['total_events']}</b></span>
          <span class="pill">pageview: {j['by_event'].get('pageview',0)}</span>
          <span class="pill">click: {j['by_event'].get('calculate_click',0)}</span>
          <span class="pill">succès: {j['by_event'].get('calculate_success',0)}</span>
          <span class="pill">erreurs: {j['by_event'].get('calculate_error',0)}</span>
        </div>
        <table>
          <thead>
            <tr><th>Date (UTC)</th><th>Pageviews</th><th>Clicks</th><th>Succès</th><th>Erreurs</th></tr>
          </thead>
          <tbody>{rows}</tbody>
        </table>
        <div class="meta" style="margin-top:12px">
          • Télécharger le CSV brut : <a href="/events.csv">/events.csv</a> — API JSON : <a href="/stats">/stats</a>
        </div>
      </div>
    </body></html>
    """
    return HTMLResponse(html)
