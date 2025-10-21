# app_pricer_cors.py
import os, json, textwrap
from typing import Any, Dict, Optional, List
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, HTMLResponse, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from pricer_engine import compute_annuity

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from psycopg.errors import OperationalError
from psycopg.errors import InterfaceError

APP_ORIGINS = [
    "*",                                 # ou limite à ton Netlify si tu veux
    "https://simulateur-price.netlify.app",
    "https://simulateur-pricer.netlify.app",
]

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

app = FastAPI(title="Simulateur Pricer API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=APP_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------- POOL DE CONNEXIONS POSTGRES ROBUSTE ----------
_pool: Optional[ConnectionPool] = None

def _build_pool() -> Optional[ConnectionPool]:
    if not DATABASE_URL:
        return None
    # Keepalives pour éviter les coupures d'inactivité
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
    global _pool
    if _pool is None:
        try:
            _pool = _build_pool()
            if _pool:
                # crée la table si besoin
                with _pool.connection() as conn:
                    conn.execute("""
                    create table if not exists events(
                        id bigserial primary key,
                        ts_utc timestamptz not null default (now() at time zone 'utc'),
                        ip text,
                        ua text,
                        ref text,
                        event text not null,
                        payload jsonb
                    );
                    """)
                print("Postgres: CONNECTED & TABLE READY")
        except Exception as e:
            print("Postgres: INIT ERROR", e)
            _pool = None
    return _pool

def _db_call(fn):
    """Décorateur de retry pour les fonctions DB (recrée le pool si besoin)."""
    async def wrapper(*args, **kwargs):
        global _pool
        for attempt in range(3):
            pool = _get_pool()
            if not pool:
                # pas de DB -> on continue silencieusement
                return None
            try:
                return await fn(*args, **kwargs, pool=pool)
            except (OperationalError, InterfaceError) as e:
                print(f"{fn.__name__} error: {e}")
                # on détruit et on reconstruit le pool puis retry
                try:
                    pool.close()
                except Exception:
                    pass
                _pool = None
            except Exception as e:
                print(f"{fn.__name__} error:", e)
                return None
        return None
    return wrapper

# ---------- SCHEMAS ----------
class ComputeIn(BaseModel):
    montant_disponible: float
    devise: str
    duree: int
    retrocessions: str  # "oui" / "non"
    frais_contrat: float = 0.0  # décimal, ex 0.001 = 0.10 %

# ---------- ROUTES ----------
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

# ---------- TRACKING EN BD ----------
def _client_ip(request: Request) -> str:
    for h in ("x-forwarded-for", "cf-connecting-ip", "x-real-ip"):
        v = request.headers.get(h) or request.client.host
        if v:
            return v.split(",")[0].strip()
    return request.client.host if request.client else ""

@app.post("/collect")
@_db_call
async def collect(request: Request, pool: ConnectionPool):
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    event = str(body.get("event") or "event")
    payload = body
    ip = _client_ip(request)
    ua = request.headers.get("user-agent", "")
    ref = request.headers.get("referer", "")

    try:
        with pool.connection() as conn:
            conn.execute(
                "insert into events (ip, ua, ref, event, payload) values (%s,%s,%s,%s,%s)",
                (ip, ua, ref, event, json.dumps(payload)),
            )
    except Exception as e:
        print("append_event_db error:", e)
    return {"ok": True}

@app.get("/events.csv")
@_db_call
async def events_csv(pool: ConnectionPool):
    try:
        with pool.connection() as conn:
            rows = conn.execute(
                "select id, ts_utc, ip, event, payload from events order by id desc limit 2000"
            ).fetchall()
        # CSV simple
        lines = ["id;ts_utc;ip;event;payload"]
        for r in rows:
            lines.append(
                f"{r['id']};{r['ts_utc']};{r['ip']};{r['event']};{json.dumps(r['payload'], ensure_ascii=False)}"
            )
        return PlainTextResponse("\n".join(lines), media_type="text/csv; charset=utf-8")
    except Exception as e:
        print("load_events_db error:", e)
        return PlainTextResponse("id;ts_utc;ip;event;payload\n", media_type="text/csv; charset=utf-8")

@app.get("/stats")
@_db_call
async def stats(days: int = 30, pool: ConnectionPool = None):
    days = max(1, min(days, 365))
    out: Dict[str, Any] = {"days": days, "by_day": [], "last": 0, "by_event": []}
    try:
        with pool.connection() as conn:
            # Agrégations avec make_interval (évite l'erreur INTERVAL $1)
            by_day = conn.execute(
                """
                select date_trunc('day', ts_utc) as day, count(*) as n
                from events
                where ts_utc >= (now() at time zone 'utc') - make_interval(days => %s)
                group by 1
                order by 1;
                """,
                (days,),
            ).fetchall()

            last = conn.execute(
                "select count(*) as n from events where ts_utc >= (now() at time zone 'utc') - interval '24 hours';"
            ).fetchone()["n"]

            by_event = conn.execute(
                """
                select event, count(*) as n
                from events
                where ts_utc >= (now() at time zone 'utc') - make_interval(days => %s)
                group by 1
                order by n desc, event asc;
                """,
                (days,),
            ).fetchall()

        out["by_day"] = [{"day": str(r["day"])[:10], "n": int(r["n"])} for r in by_day]
        out["last"] = int(last)
        out["by_event"] = [{"event": r["event"], "n": int(r["n"])} for r in by_event]
    except Exception as e:
        print("load_events_db error:", e)
    return JSONResponse(out)

@app.get("/stats.html", include_in_schema=False)
def stats_html():
    # petit dashboard minimal (inchangé)
    html = """
    <!doctype html><html lang="fr"><meta charset="utf-8">
    <title>Stats</title>
    <style>
      body{font-family:system-ui,Segoe UI,Arial;margin:24px;background:#f7f7fb;color:#0f172a}
      .wrap{max-width:980px;margin:auto}
      h1{margin:0 0 8px}
      .row{display:flex;gap:16px;flex-wrap:wrap}
      .card{background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:16px;flex:1}
      .kpi{font-size:28px;font-weight:800}
      table{width:100%;border-collapse:collapse;margin-top:8px}
      th,td{border-bottom:1px solid #eef2f7;padding:8px;text-align:left}
      .ctrl{margin-bottom:12px}
      .dl{margin-top:12px;display:inline-block}
      canvas{width:100%;height:240px}
    </style>
    <div class="wrap">
      <h1>Statistiques d’usage</h1>
      <div class="ctrl">
        Période :
        <select id="days">
          <option value="7">7 jours</option>
          <option value="30" selected>30 jours</option>
          <option value="90">90 jours</option>
        </select>
        <button id="refresh">Actualiser</button>
        <a class="dl" href="/events.csv" target="_blank">Télécharger le CSV des événements</a>
      </div>
      <div class="row">
        <div class="card"><div>Événements sur 24h</div><div id="kpi" class="kpi">–</div></div>
        <div class="card" style="flex:3">
          <div>Événements par jour</div>
          <canvas id="chart"></canvas>
        </div>
      </div>
      <div class="card">
        <div>Par type d’événement</div>
        <table><thead><tr><th>Événement</th><th>Compteur</th></tr></thead><tbody id="byEvent"></tbody></table>
      </div>
    </div>
    <script>
      const ctx = document.getElementById('chart').getContext('2d');
      let chart;
      async function load() {
        const days = document.getElementById('days').value;
        const r = await fetch('/stats?days=' + days);
        const data = await r.json();
        document.getElementById('kpi').textContent = data.last ?? 0;
        const labels = data.by_day.map(x => x.day);
        const values = data.by_day.map(x => x.n);
        if (chart) chart.destroy();
        chart = new Chart(ctx, {
          type: 'line',
          data: { labels, datasets: [{ label:'Événements', data: values }] },
          options: { responsive:true, maintainAspectRatio:false, scales:{ y:{ beginAtZero:true, precision:0 } } }
        });
        const tb = document.getElementById('byEvent');
        tb.innerHTML = (data.by_event||[]).map(x => '<tr><td>'+x.event+'</td><td>'+x.n+'</td></tr>').join('');
      }
      document.getElementById('refresh').onclick = load;
    </script>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <script>load()</script>
    """
    return HTMLResponse(textwrap.dedent(html))

# ---------- LIFECYCLE ----------
@app.on_event("startup")
def on_startup():
    _get_pool()
