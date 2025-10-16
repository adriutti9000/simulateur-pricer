# app_pricer_cors.py
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    RedirectResponse, Response, FileResponse, HTMLResponse, JSONResponse
)
from pydantic import BaseModel
from typing import Literal, Optional, Dict, List
from datetime import datetime, timezone, date, timedelta
import os, csv

from pricer_engine import compute_annuity  # moteur inchangé

# =========================
#  Config CORS
# =========================
ALLOWED_ORIGINS = [
    "https://simulateur-price.netlify.app",
    "http://localhost:8000",
    "http://127.0.0.1:8000",
]

app = FastAPI(title="Simulateur Pricer")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# =========================
#  Modèles
# =========================
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

class CollectEvent(BaseModel):
    event: Literal["pageview", "calculate_click", "calculate_success", "calculate_error"]
    montant: Optional[float] = None
    devise: Optional[Literal["EUR", "USD"]] = None
    duree: Optional[int] = None
    retro: Optional[Literal["oui", "non"]] = None
    support: Optional[Literal["assurance-vie", "compte-titres"]] = None
    frais_contrat: Optional[float] = None
    rente: Optional[float] = None
    error: Optional[str] = None

# =========================
#  Persistance : Postgres (Neon) OU CSV local (/tmp)
# =========================
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

CSV_PATH = os.environ.get("EVENTS_CSV", "/tmp/events_log.csv")
CSV_FIELDS = ["ts_utc","ip","ua","event","montant","devise","duree","retro","support","frais_contrat","rente","error"]

# --- Postgres (psycopg 3)
use_db = False
conn = None
if DATABASE_URL:
    try:
        import psycopg  # psycopg[binary]
        conn = psycopg.connect(DATABASE_URL, autocommit=True)
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS events (
                    id BIGSERIAL PRIMARY KEY,
                    ts_utc TIMESTAMPTZ NOT NULL,
                    ip TEXT,
                    ua TEXT,
                    event TEXT NOT NULL,
                    montant NUMERIC,
                    devise TEXT,
                    duree INT,
                    retro TEXT,
                    support TEXT,
                    frais_contrat NUMERIC,
                    rente NUMERIC,
                    error TEXT
                );
            """)
        use_db = True
        print("Postgres: CONNECTED & TABLE READY")
    except Exception as e:
        print("Postgres disabled:", e)
        use_db = False

def append_event_db(row: dict):
    if not use_db or conn is None:
        return
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO events
                (ts_utc, ip, ua, event, montant, devise, duree, retro, support, frais_contrat, rente, error)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s);
            """, (
                row.get("ts_utc"),
                row.get("ip"),
                row.get("ua"),
                row.get("event"),
                row.get("montant"),
                row.get("devise"),
                row.get("duree"),
                row.get("retro"),
                row.get("support"),
                row.get("frais_contrat"),
                row.get("rente"),
                row.get("error"),
            ))
    except Exception as e:
        print("append_event_db error:", e)

def load_events_db(days: Optional[int] = None) -> List[Dict[str, str]]:
    if not use_db or conn is None:
        return []
    try:
        with conn.cursor() as cur:
            if days:
                cur.execute("""
                    SELECT ts_utc, ip, ua, event, montant, devise, duree, retro, support, frais_contrat, rente, error
                    FROM events
                    WHERE ts_utc >= (NOW() AT TIME ZONE 'UTC') - INTERVAL '%s days'
                    ORDER BY ts_utc ASC;
                """, (days,))
            else:
                cur.execute("""
                    SELECT ts_utc, ip, ua, event, montant, devise, duree, retro, support, frais_contrat, rente, error
                    FROM events
                    ORDER BY ts_utc ASC;
                """)
            rows = cur.fetchall()
        result = []
        for r in rows:
            result.append({
                "ts_utc": r[0].isoformat(),
                "ip": r[1] or "",
                "ua": r[2] or "",
                "event": r[3] or "",
                "montant": str(r[4]) if r[4] is not None else "",
                "devise": r[5] or "",
                "duree": str(r[6]) if r[6] is not None else "",
                "retro": r[7] or "",
                "support": r[8] or "",
                "frais_contrat": str(r[9]) if r[9] is not None else "",
                "rente": str(r[10]) if r[10] is not None else "",
                "error": r[11] or "",
            })
        return result
    except Exception as e:
        print("load_events_db error:", e)
        return []

# --- Fallback local CSV
def append_event_csv(row: dict):
    os.makedirs(os.path.dirname(CSV_PATH), exist_ok=True)
    file_exists = os.path.exists(CSV_PATH)
    with open(CSV_PATH, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if not file_exists:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in CSV_FIELDS})

def load_events_csv() -> List[Dict[str, str]]:
    if not os.path.exists(CSV_PATH):
        return []
    with open(CSV_PATH, "r", newline="", encoding="utf-8") as f:
        r = csv.DictReader(f)
        return list(r)

# =========================
#  Routes confort
# =========================
@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/docs")

@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return Response(status_code=204)

@app.get("/health")
def health():
    return {"status": "ok", "db": use_db}

# =========================
#  Calcul
# =========================
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

# =========================
#  Tracking (collect)
# =========================
@app.post("/collect")
async def collect(event: CollectEvent, request: Request):
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
    # DB si dispo, sinon CSV local
    if use_db:
        append_event_db(row)
    else:
        append_event_csv(row)
    return {"ok": True}

# =========================
#  Export CSV & Stats
# =========================
@app.get("/events.csv", response_class=FileResponse)
def download_events_csv():
    if use_db:
        # Génère un CSV en mémoire à partir de la DB → fichier temporaire
        tmp_path = "/tmp/events_export.csv"
        rows = load_events_db()
        with open(tmp_path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
            w.writeheader()
            for r in rows:
                w.writerow(r)
        return FileResponse(tmp_path, media_type="text/csv", filename="events_log.csv")
    # fallback local
    if not os.path.exists(CSV_PATH):
        os.makedirs(os.path.dirname(CSV_PATH), exist_ok=True)
        with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
            w.writeheader()
    return FileResponse(CSV_PATH, media_type="text/csv", filename="events_log.csv")

@app.get("/stats", response_class=JSONResponse)
def stats(days: int = 30):
    days = max(1, min(days, 365))
    if use_db:
        data = load_events_db(days=days)
    else:
        data = load_events_csv()

    total = len(data)
    by_event: Dict[str, int] = {}
    for row in data:
        by_event[row["event"]] = by_event.get(row["event"], 0) + 1

    end = date.today()
    start = end - timedelta(days=days - 1)
    per_day = { (start + timedelta(d)): {"pageview":0,"calculate_click":0,"calculate_success":0,"calculate_error":0}
                for d in range(days) }

    for row in data:
        try:
            ts = datetime.fromisoformat(row["ts_utc"].replace("Z","+00:00")).date()
        except Exception:
            continue
        if start <= ts <= end and row["event"] in per_day[ts]:
            per_day[ts][row["event"]] += 1

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
          {"<div style='margin-top:6px;color:#475569'>Base de données: ACTIVÉE</div>" if use_db else "<div style='margin-top:6px;color:#475569'>Base de données: désactivée (historique local)</div>"}
        </div>
      </div>
    </body></html>
    """
    return HTMLResponse(html)
