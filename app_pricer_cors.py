# app_pricer_cors.py
import os, json, textwrap, time
from typing import Any, Dict, Optional, Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, HTMLResponse, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from pricer_engine import compute_annuity

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from psycopg.errors import OperationalError, InterfaceError

# -------------------- Config CORS --------------------
app = FastAPI(title="Simulateur Pricer API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],            # limite à ton domaine Netlify si tu veux
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -------------------- DB Pool Neon --------------------
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
_pool: Optional[ConnectionPool] = None

def _build_pool() -> Optional[ConnectionPool]:
    if not DATABASE_URL:
        return None
    # keepalives pour limiter les coupures
    conninfo = (
        DATABASE_URL
        + ("&" if "?" in DATABASE_URL else "?")
        + "sslmode=require&keepalives=1&keepalives_idle=30&keepalives_interval=10&keepalives_count=5"
    )
    return ConnectionPool(
        conninfo=conninfo,
        min_size=1,
        max_size=5,
        kwargs={"autocommit": True, "row_factory": dict_row},
        timeout=30,
    )

def _get_pool() -> Optional[ConnectionPool]:
    """Retourne un pool prêt. Recrée si besoin et initialise la table."""
    global _pool
    if _pool is None:
        try:
            _pool = _build_pool()
            if _pool:
                with _pool.connection() as conn:
                    conn.execute("""
                        CREATE TABLE IF NOT EXISTS events(
                            id BIGSERIAL PRIMARY KEY,
                            ts_utc TIMESTAMPTZ NOT NULL DEFAULT (NOW() AT TIME ZONE 'UTC'),
                            ip  TEXT,
                            ua  TEXT,
                            ref TEXT,
                            event   TEXT NOT NULL,
                            payload JSONB
                        );
                    """)
                print("Postgres: CONNECTED & TABLE READY")
        except Exception as e:
            print("Postgres: INIT ERROR", e)
            _pool = None
    return _pool

def _with_db(action: Callable[[psycopg.Connection], Any]) -> Any:
    """
    Exécute une action DB avec retries si la connexion est fermée.
    Ne passe PAS de paramètre 'pool' aux routes => pas d’erreur FastAPI.
    """
    global _pool
    for attempt in range(3):
        pool = _get_pool()
        if not pool:
            return None
        try:
            with pool.connection() as conn:
                return action(conn)
        except (OperationalError, InterfaceError) as e:
            # on reset le pool et on retente
            print("DB error, resetting pool:", e)
            try:
                pool.close()
            except Exception:
                pass
            _pool = None
            time.sleep(0.2 * (attempt + 1))
        except Exception as e:
            print("DB action error:", e)
            return None
    return None

# -------------------- Schémas --------------------
class ComputeIn(BaseModel):
    montant_disponible: float
    devise: str
    duree: int
    retrocessions: str     # "oui" / "non"
    frais_contrat: float = 0.0  # décimal (ex: 0.001 = 0,10 %)

# -------------------- Routes --------------------
@app.get("/", include_in_schema=False)
def root():
    return JSONResponse({"ok": True, "docs": "/docs"})

@app.post("/compute")
def compute(inp: ComputeIn):
    include_retro = (inp.retrocessions.lower() == "oui")
    out = compute_annuity(
        amount=inp.montant_disponible,
        currency=inp.devise,
        years=inp.duree,
        include_retro=include_retro,
        extra_contract_fee=inp.frais_contrat or 0.0,
    )
    return out

def _client_ip(request: Request) -> str:
    for h in ("x-forwarded-for", "cf-connecting-ip", "x-real-ip"):
        v = request.headers.get(h) or (request.client.host if request.client else "")
        if v:
            return v.split(",")[0].strip()
    return request.client.host if request.client else ""

@app.post("/collect")
async def collect(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    event = str(body.get("event") or "event")
    ip = _client_ip(request)
    ua = request.headers.get("user-agent", "")
    ref = request.headers.get("referer", "")

    def _insert(conn: psycopg.Connection):
        conn.execute(
            "INSERT INTO events (ip, ua, ref, event, payload) VALUES (%s, %s, %s, %s, %s)",
            (ip, ua, ref, event, json.dumps(body)),
        )

    _with_db(_insert)
    return {"ok": True}

@app.get("/events.csv")
def events_csv():
    def _load(conn: psycopg.Connection):
        return conn.execute(
            "SELECT id, ts_utc, ip, event, payload FROM events ORDER BY id DESC LIMIT 2000"
        ).fetchall()

    rows = _with_db(_load) or []
    lines = ["id;ts_utc;ip;event;payload"]
    for r in rows:
        lines.append(
            f"{r['id']};{r['ts_utc']};{r['ip']};{r['event']};{json.dumps(r['payload'], ensure_ascii=False)}"
        )
    return PlainTextResponse("\n".join(lines), media_type="text/csv; charset=utf-8")

@app.get("/stats")
def stats(days: int = 30):
    days = max(1, min(days, 365))
    out = {"days": days, "by_day": [], "last": 0, "by_event": []}

    def _query(conn: psycopg.Connection):
        by_day = conn.execute(
            """
            SELECT date_trunc('day', ts_utc) AS day, COUNT(*) AS n
            FROM events
            WHERE ts_utc >= (NOW() AT TIME ZONE 'UTC') - make_interval(days => %s)
            GROUP BY 1
            ORDER BY 1;
            """,
            (days,),
        ).fetchall()
        last = conn.execute(
            "SELECT COUNT(*) AS n FROM events WHERE ts_utc >= (NOW() AT TIME ZONE 'UTC') - interval '24 hours';"
        ).fetchone()["n"]
        by_event = conn.execute(
            """
            SELECT event, COUNT(*) AS n
            FROM events
            WHERE ts_utc >= (NOW() AT TIME ZONE 'UTC') - make_interval(days => %s)
            GROUP BY 1
            ORDER BY n DESC, event ASC;
            """,
            (days,),
        ).fetchall()
        return by_day, last, by_event

    res = _with_db(_query)
    if res:
        by_day, last, by_event = res
        out["by_day"] = [{"day": str(r["day"])[:10], "n": int(r["n"])} for r in by_day]
        out["last"] = int(last)
        out["by_event"] = [{"event": r["event"], "n": int(r["n"])} for r in by_event]

    return JSONResponse(out)

@app.get("/stats.html", include_in_schema=False)
def stats_html():
    html = """
    <!doctype html><html lang="fr"><meta charset="utf-8">
    <title>Stats simulateur</title>
    <style>
      body{font-family:Segoe UI,system-ui,Arial;margin:24px;background:#f5f7fb;color:#0f172a}
      .wrap{max-width:980px;margin:auto}
      h1{margin:0 0 10px}
      .toolbar{display:flex;gap:10px;align-items:center;margin:10px 0 18px}
      select,button,a{padding:8px 12px;border-radius:8px;border:1px solid #dbe3f3;font-size:14px;background:#fff;text-decoration:none;color:#0f172a}
      button{background:#1d5fd3;color:#fff;border:none}
      button:hover{background:#184fb0}
      .grid{display:grid;grid-template-columns:1fr 2fr;gap:16px}
      .card{background:#fff;border:1px solid #e6eaf2;border-radius:12px;padding:16px}
      .kpi{font-size:28px;font-weight:800}
      table{width:100%;border-collapse:collapse;margin-top:8px}
      th,td{border-bottom:1px solid #eef2f7;padding:8px;text-align:left}
      canvas{width:100%;height:260px}
    </style>
    <div class="wrap">
      <h1>Statistiques d’usage</h1>
      <div class="toolbar">
        <span>Période :</span>
        <select id="days">
          <option value="7">7 jours</option>
          <option value="30" selected>30 jours</option>
          <option value="90">90 jours</option>
        </select>
        <button id="refresh">Actualiser</button>
        <a href="/events.csv" target="_blank">Télécharger le CSV</a>
      </div>

      <div class="grid">
        <div class="card">
          <div>Événements sur 24h</div>
          <div id="kpi" class="kpi">–</div>
        </div>
        <div class="card">
          <div>Événements par jour</div>
          <canvas id="chart"></canvas>
        </div>
      </div>

      <div class="card" style="margin-top:16px">
        <div>Par type d’événement</div>
        <table>
          <thead><tr><th>Événement</th><th>Compteur</th></tr></thead>
          <tbody id="byEvent"></tbody>
        </table>
      </div>
    </div>

    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <script>
      const ctx = document.getElementById('chart').getContext('2d');
      let chart;
      async function load(){
        const days = document.getElementById('days').value;
        const r = await fetch('/stats?days=' + days);
        const data = await r.json();

        document.getElementById('kpi').textContent = data.last ?? 0;

        const labels = (data.by_day||[]).map(x => x.day);
        const values = (data.by_day||[]).map(x => x.n);

        if (chart) chart.destroy();
        chart = new Chart(ctx, {
          type: 'line',
          data: { labels, datasets:[{ label:'Événements', data: values }] },
          options: { responsive:true, maintainAspectRatio:false, scales:{ y:{ beginAtZero:true, ticks:{ precision:0 } } } }
        });

        document.getElementById('byEvent').innerHTML =
          (data.by_event||[]).map(x => `<tr><td>${x.event}</td><td>${x.n}</td></tr>`).join('');
      }
      document.getElementById('refresh').onclick = load;
      load();
    </script>
    """
    return HTMLResponse(textwrap.dedent(html))

# -------------------- Startup --------------------
@app.on_event("startup")
def on_startup():
    _get_pool()
