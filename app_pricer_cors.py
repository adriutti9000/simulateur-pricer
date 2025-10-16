from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    RedirectResponse, Response, FileResponse, HTMLResponse, JSONResponse
)
from pydantic import BaseModel
from typing import Literal, Optional, Dict, List
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
import os, csv, tempfile

from pricer_engine import compute_annuity  # ton moteur existant

# ---------------- CORS ----------------
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

# ---------------- DB (Neon) ----------------
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
use_db = False
conn = None

def db_connect():
    global conn, use_db
    if not DATABASE_URL:
        use_db = False
        return
    try:
        import psycopg  # psycopg[binary]
        conn = psycopg.connect(DATABASE_URL, autocommit=True)
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS events (
                    id BIGSERIAL PRIMARY KEY,
                    ts_utc TIMESTAMPTZ NOT NULL DEFAULT (NOW() AT TIME ZONE 'UTC'),
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

db_connect()

# ---------------- Schémas ----------------
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

# ---------------- Helpers DB ----------------
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
    """
    Corrigé : on n'utilise plus INTERVAL $1 (non paramétrable en psycopg)
    mais make_interval(days => %s) qui accepte un entier.
    """
    if not use_db or conn is None:
        return []
    try:
        with conn.cursor() as cur:
            if days:
                cur.execute("""
                    SELECT ts_utc, ip, ua, event, montant, devise, duree, retro, support, frais_contrat, rente, error
                    FROM events
                    WHERE ts_utc >= (NOW() AT TIME ZONE 'UTC') - make_interval(days => %s)
                    ORDER BY ts_utc ASC;
                """, (int(days),))
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
                "montant": float(r[4]) if r[4] is not None else None,
                "devise": r[5] or "",
                "duree": int(r[6]) if r[6] is not None else None,
                "retro": r[7] or "",
                "support": r[8] or "",
                "frais_contrat": float(r[9]) if r[9] is not None else None,
                "rente": float(r[10]) if r[10] is not None else None,
                "error": r[11] or "",
            })
        return result
    except Exception as e:
        print("load_events_db error:", e)
        return []

# ---------------- Routes confort ----------------
@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/docs")

@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return Response(status_code=204)

@app.get("/health")
def health():
    return {"status": "ok", "db": use_db}

# ---------------- /compute ----------------
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

# ---------------- /collect ----------------
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
    if use_db:
        append_event_db(row)
    return {"ok": True}

# ---------------- /events.csv ----------------
@app.get("/events.csv")
def export_csv():
    rows = load_events_db() if use_db else []
    fd, tmp_path = tempfile.mkstemp(prefix="events_", suffix=".csv")
    os.close(fd)
    with open(tmp_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "ts_utc","ip","ua","event","montant","devise","duree",
            "retro","support","frais_contrat","rente","error"
        ])
        for r in rows:
            w.writerow([
                r["ts_utc"], r["ip"], r["ua"], r["event"], r["montant"], r["devise"],
                r["duree"], r["retro"], r["support"], r["frais_contrat"], r["rente"], r["error"]
            ])
    return FileResponse(tmp_path, media_type="text/csv", filename="events_log.csv")

# ---------------- /stats (JSON) ----------------
@app.get("/stats", response_class=JSONResponse)
def stats(days: int = 30):
    days = max(1, min(days, 365))
    data = load_events_db(days=days) if use_db else []

    pageviews = sum(1 for r in data if r["event"] == "pageview")
    clicks    = sum(1 for r in data if r["event"] == "calculate_click")
    success   = sum(1 for r in data if r["event"] == "calculate_success")
    errors    = sum(1 for r in data if r["event"] == "calculate_error")

    lux = ZoneInfo("Europe/Luxembourg")
    end = datetime.now(lux).date()
    start = end - timedelta(days=days - 1)

    per_day = {
        (start + timedelta(d)).isoformat(): {"pageviews":0,"clicks":0,"success":0,"errors":0}
        for d in range(days)
    }

    for r in data:
        try:
            ts = datetime.fromisoformat(r["ts_utc"].replace("Z", "+00:00"))
        except Exception:
            continue
        ts_local = ts.astimezone(lux)
        dkey = ts_local.date().isoformat()
        if dkey in per_day:
            if r["event"] == "pageview": per_day[dkey]["pageviews"] += 1
            elif r["event"] == "calculate_click": per_day[dkey]["clicks"] += 1
            elif r["event"] == "calculate_success": per_day[dkey]["success"] += 1
            elif r["event"] == "calculate_error": per_day[dkey]["errors"] += 1

    daily = [{"date": d, **per_day[d]} for d in sorted(per_day.keys())]

    return {
        "range_days": days,
        "pageviews": pageviews,
        "clicks": clicks,
        "success": success,
        "errors": errors,
        "daily": daily
    }

# ---------------- /stats.html ----------------
@app.get("/stats.html", response_class=FileResponse)
def serve_stats_html():
    file_path = os.path.join(os.path.dirname(__file__), "stats.html")
    return FileResponse(file_path, media_type="text/html")
