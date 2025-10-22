# -*- coding: utf-8 -*-
# app_pricer_cors.py
# API FastAPI + tracking PostgreSQL (Neon) + dashboard /stats.html intégré
# Dépendances (requirements.txt) :
#   fastapi
#   uvicorn[standard]
#   pydantic>=2
#   psycopg[binary,pool]==3.2.10

import os, json, time, textwrap
from typing import Any, Optional, Callable

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse, PlainTextResponse
from pydantic import BaseModel

from pricer_engine import compute_annuity

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from psycopg.errors import OperationalError, InterfaceError

# -------------- CORS --------------
app = FastAPI(title="Simulateur Pricer API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # restreins ici à ton domaine Netlify si tu veux
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -------------- DB pool (Neon) --------------
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
_pool: Optional[ConnectionPool] = None

def _build_pool() -> Optional[ConnectionPool]:
    if not DATABASE_URL:
        return None
    conninfo = (
        DATABASE_URL
        + ("&" if "?" in DATABASE_URL else "?")
        + "sslmode=require&keepalives=1&keepalives_idle=30"
          "&keepalives_interval=10&keepalives_count=5"
    )
    return ConnectionPool(
        conninfo=conninfo,
        min_size=1,
        max_size=5,
        kwargs={"autocommit": True, "row_factory": dict_row},
        timeout=30,
    )

def _ensure_schema(conn: psycopg.Connection) -> None:
    # table minimale
    conn.execute("""
        CREATE TABLE IF NOT EXISTS events(
            id BIGSERIAL PRIMARY KEY,
            ts_utc TIMESTAMPTZ NOT NULL DEFAULT (NOW() AT TIME ZONE 'UTC'),
            ip  TEXT,
            ua  TEXT,
            event   TEXT NOT NULL,
            payload JSONB
        );
    """)
    # migrations douces
    conn.execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS ref TEXT;")
    conn.execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS payload JSONB;")
    conn.execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS ua TEXT;")
    conn.execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS ip TEXT;")
    conn.execute("""
        ALTER TABLE events
        ALTER COLUMN ts_utc SET DEFAULT (NOW() AT TIME ZONE 'UTC');
    """)

def _get_pool() -> Optional[ConnectionPool]:
    global _pool
    if _pool is None:
        try:
            _pool = _build_pool()
            if _pool:
                with _pool.connection() as conn:
                    _ensure_schema(conn)
                print("Postgres: CONNECTED & TABLE READY (migrée)")
        except Exception as e:
            print("Postgres: INIT ERROR", e)
            _pool = None
    return _pool

def _with_db(action: Callable[[psycopg.Connection], Any]) -> Any:
    """Exécute une action DB avec retries si Neon coupe la connexion idle."""
    global _pool
    for attempt in range(3):
        pool = _get_pool()
        if not pool:
            return None
        try:
            with pool.connection() as conn:
                return action(conn)
        except (OperationalError, InterfaceError) as e:
            print("DB error, resetting pool:", e)
            try:
                pool.close()
            except Exception:
                pass
            _pool = None
            time.sleep(0.25 * (attempt + 1))
        except Exception as e:
            print("DB action error:", e)
            return None
    return None

# -------------- Schéma /compute --------------
class ComputeIn(BaseModel):
    montant_disponible: float
    devise: str
    duree: int
    retrocessions: str     # "oui" / "non"
    frais_contrat: float = 0.0  # 0.001 = 0,10 %

# -------------- Routes coeur --------------
@app.get("/health", include_in_schema=False)
def health():
    ok = _with_db(lambda c: 1) is not None
    return JSONResponse({"ok": True, "db": ok})

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

# -------------- Exports & stats API --------------
@app.get("/events.csv")
def events_csv():
    def _load(conn: psycopg.Connection):
        return conn.execute(
            "SELECT id, ts_utc, ip, event, payload FROM events ORDER BY id DESC LIMIT 5000"
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
    out = {"days": days, "by_day": [], "last": 0, "by_event": [], "total": 0, "unique_ips": 0}

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
        total = conn.execute(
            "SELECT COUNT(*) AS n FROM events WHERE ts_utc >= (NOW() AT TIME ZONE 'UTC') - make_interval(days => %s);",
            (days,),
        ).fetchone()["n"]
        unique_ips = conn.execute(
            "SELECT COUNT(DISTINCT ip) AS n FROM events WHERE ts_utc >= (NOW() AT TIME ZONE 'UTC') - make_interval(days => %s);",
            (days,),
        ).fetchone()["n"]
        return by_day, last, by_event, total, unique_ips

    res = _with_db(_query)
    if res:
        by_day, last, by_event, total, unique_ips = res
        out["by_day"] = [{"day": str(r["day"])[:10], "n": int(r["n"])} for r in by_day]
        out["last"] = int(last)
        out["by_event"] = [{"event": r["event"], "n": int(r["n"])} for r in by_event]
        out["total"] = int(total)
        out["unique_ips"] = int(unique_ips)

    return JSONResponse(out, headers={"Cache-Control": "no-store"})

# -------------- Dashboard /stats.html (sans f-string) --------------
@app.get("/stats.html", include_in_schema=False)
def stats_html():
    html = """
<!doctype html><html lang="fr"><meta charset="utf-8">
<title>Stats simulateur v3.2</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Cache-Control" content="no-store" />
<style>
  :root{
    --bg:#0b1220; --bg2:#0f172a; --card:#0f1b31; --edge:#213655; --text:#e6eefc; --muted:#94a3b8;
    --brand:#1d5fd3;
  }
  *{box-sizing:border-box}
  body{margin:0; font-family:Inter, system-ui, -apple-system, Segoe UI, Roboto, Arial; color:var(--text); background:linear-gradient(180deg,var(--bg), var(--bg2));}
  .wrap{max-width:1150px; margin:28px auto; padding:0 18px;}
  header.banner{background:linear-gradient(90deg,#1d5fd3, #0ea5e9); border-radius:14px; padding:16px 18px; box-shadow:0 10px 30px rgba(0,0,0,.25);}
  header .title{display:flex; align-items:baseline; gap:10px;}
  header h1{margin:0; font-size:20px; font-weight:800; color:#fff}
  header .ver{opacity:.85; color:#e2efff; font-size:12px; padding:2px 8px; border-radius:999px; background:rgba(255,255,255,.15)}
  .toolbar{display:flex; gap:10px; align-items:center; flex-wrap:wrap; margin:14px 0 18px}
  select,button,a{padding:9px 12px; border-radius:10px; border:1px solid var(--edge); font-size:14px; background:#0d1a30; color:var(--text); text-decoration:none}
  button{background:var(--brand); border:1px solid transparent}
  button:hover{filter:brightness(1.08)}
  a:hover{filter:brightness(1.12)}
  .grid{display:grid; grid-template-columns:1.1fr 1.9fr; gap:16px}
  @media (max-width:980px){ .grid{grid-template-columns:1fr} }
  .card{background:var(--card); border:1px solid var(--edge); border-radius:14px; padding:16px; box-shadow:0 6px 20px rgba(0,0,0,.2)}
  .kpis{display:grid; grid-template-columns:repeat(3,1fr); gap:12px}
  .kpi{display:flex; flex-direction:column; gap:6px; background:#0c1527; border:1px solid var(--edge); border-radius:12px; padding:14px}
  .kpi .label{color:var(--muted); font-size:12px}
  .kpi .val{font-size:28px; font-weight:800; letter-spacing:.2px}
  .muted{color:var(--muted)}
  .chart-box{height:340px; position:relative}
  .chart-box canvas{position:absolute; inset:0; width:100% !important; height:100% !important}
  table{width:100%; border-collapse:collapse; table-layout:fixed}
  th, td{padding:10px; border-bottom:1px solid var(--edge); text-align:left; font-size:14px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis}
  th{position:sticky; top:0; background:#0e1a2d; z-index:1}
  .table-wrap{max-height:420px; overflow:auto; border:1px solid var(--edge); border-radius:12px}
  .cols-2{width:38%} .cols-1{width:62%}
  .status{margin-left:auto; font-size:13px; color:var(--muted)}
</style>

<div class="wrap">
  <header class="banner">
    <div class="title">
      <h1>Statistiques d’usage</h1>
      <span class="ver">v3.2</span>
    </div>
  </header>

  <div class="toolbar">
    <span class="muted">Période :</span>
    <select id="days">
      <option value="7">7 jours</option>
      <option value="30" selected>30 jours</option>
      <option value="90">90 jours</option>
    </select>
    <button id="refresh">Actualiser</button>
    <a href="/events.csv" target="_blank">Télécharger le CSV</a>
    <span id="status" class="status"></span>
  </div>

  <div class="kpis">
    <div class="kpi">
      <div class="label">Événements (24h)</div>
      <div id="kpiLast" class="val">–</div>
    </div>
    <div class="kpi">
      <div class="label">Total sur période</div>
      <div id="kpiTotal" class="val">–</div>
    </div>
    <div class="kpi">
      <div class="label">Visiteurs (IP) uniques</div>
      <div id="kpiIPs" class="val">–</div>
    </div>
  </div>

  <div class="grid" style="margin-top:14px">
    <div class="card">
      <div class="muted" style="margin-bottom:8px">Événements par jour</div>
      <div class="chart-box"><canvas id="chart"></canvas></div>
    </div>
    <div class="card">
      <div class="muted" style="margin-bottom:8px">Par type d’événement</div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr><th class="cols-1">Événement</th><th class="cols-2">Compteur</th></tr>
          </thead>
          <tbody id="byEvent"></tbody>
        </table>
      </div>
      <div id="period" class="muted" style="margin-top:10px"></div>
    </div>
  </div>
</div>

<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<script>
  const statusEl = document.getElementById('status');
  const daysSel  = document.getElementById('days');
  const kLast = document.getElementById('kpiLast');
  const kTot  = document.getElementById('kpiTotal');
  const kIPs  = document.getElementById('kpiIPs');
  const byEventEl = document.getElementById('byEvent');
  const periodEl  = document.getElementById('period');
  let chart;

  function nb(x){ return new Intl.NumberFormat('fr-FR').format(x||0); }
  function setStatus(t){ statusEl.textContent = t || ""; }

  async function load(){
    setStatus("Chargement…");
    const days = daysSel.value;
    try{
      const r = await fetch('/stats?days=' + days + '&_=' + Date.now(), { cache:'no-store' });
      const data = await r.json();

      kLast.textContent = nb(data.last);
      kTot.textContent  = nb(data.total);
      kIPs.textContent  = nb(data.unique_ips);

      if ((data.by_day||[]).length){
        const a = data.by_day[0].day, b = data.by_day[data.by_day.length-1].day;
        periodEl.textContent = `Période affichée : ${a} → ${b}`;
      } else {
        periodEl.textContent = "Aucune donnée sur la période.";
      }

      const labels = (data.by_day||[]).map(x => x.day);
      const values = (data.by_day||[]).map(x => x.n);
      if (chart) chart.destroy();
      chart = new Chart(document.getElementById('chart'), {
        type: 'line',
        data: { labels, datasets:[{ label:'Événements', data: values, tension:.25 }] },
        options: {
          responsive:true, maintainAspectRatio:false,
          plugins:{ legend:{ display:false } },
          scales:{ y:{ beginAtZero:true, ticks:{ precision:0 } } }
        }
      });

      byEventEl.innerHTML = (data.by_event||[]).map(x => `
        <tr><td title="${x.event||''}">${x.event||''}</td><td>${nb(x.n)}</td></tr>
      `).join('') || `<tr><td colspan="2" class="muted">Aucune donnée.</td></tr>`;

      setStatus("");
    }catch(e){
      console.error(e);
      setStatus("Erreur de chargement");
      byEventEl.innerHTML = `<tr><td colspan="2" style="color:#fca5a5">Impossible de charger les données.</td></tr>`;
    }
  }

  daysSel.addEventListener('change', load);
  document.getElementById('refresh').onclick = load;
  load();
</script>
"""
    return HTMLResponse(textwrap.dedent(html), headers={"Cache-Control": "no-store"})

# -------------- Startup --------------
@app.on_event("startup")
def on_startup():
    _get_pool()
