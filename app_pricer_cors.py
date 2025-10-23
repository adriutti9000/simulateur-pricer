# -*- coding: utf-8 -*-
# app_pricer_cors.py
# API FastAPI + tracking PostgreSQL (Neon) + dashboard /stats.html

import os, json, time, textwrap
from typing import Any, Optional, Callable

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse, PlainTextResponse
from pydantic import BaseModel

from pricer_engine import compute_annuity  # ⚠️ formules inchangées

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from psycopg.errors import OperationalError, InterfaceError

# -------------------- CORS --------------------
app = FastAPI(title="Simulateur Pricer API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # (optionnel) restreins à ton domaine Netlify
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -------------------- DB pool (Neon) --------------------
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
        max_size=3,
        kwargs={
            "autocommit": True,
            "prepare_threshold": 0,
            "row_factory": dict_row,  # fetch* -> dicts
        },
    )

def _ensure_schema(conn: psycopg.Connection) -> None:
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
                print("Postgres: CONNECTED & TABLE READY")
        except Exception as e:
            print("Postgres: INIT ERROR", e)
            _pool = None
    return _pool

def _with_db(action: Callable[[psycopg.Connection], Any]) -> Any:
    """Exécute une action DB avec retries si Neon coupe la connexion idle."""
    global _pool
    for attempt in range(3):
        try:
            pool = _get_pool()
            if not pool:
                return None
            with pool.connection() as conn:
                return action(conn)
        except (OperationalError, InterfaceError) as e:
            print("DB retry", attempt+1, e)
            time.sleep(0.4 * (attempt + 1))
    return None

# -------------------- Schéma I/O --------------------
class ComputeIn(BaseModel):
    montant_disponible: float
    devise: str
    duree: int
    retrocessions: str           # "oui" / "non"
    frais_contrat: float = 0.0   # 0.001 = 0,10 %

# -------------------- Routes coeur --------------------
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
        amount=float(inp.montant_disponible),
        currency=inp.devise,
        years=int(inp.duree),
        include_retro=include_retro,
        extra_contract_fee=float(inp.frais_contrat),
    )
    return JSONResponse(out)

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
    ref = request.headers.get("referer", "") or request.query_params.get("ref") or ""
    def _insert(conn: psycopg.Connection):
        conn.execute(
            "INSERT INTO events (ip, ua, ref, event, payload) VALUES (%s, %s, %s, %s, %s)",
            (ip, ua, ref, event, json.dumps(body)),
        )
    _with_db(_insert)
    return {"ok": True}

# -------------------- Exports & stats API --------------------
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
    """
    Renvoie:
      - labels: ["YYYY-MM-DD", ...] (tous les jours, zéros inclus)
      - visits_by_day: nb de pageview/jour
      - sims_by_day:   nb de calculate_click/jour
      - by_event:   top événements bruts (comme avant)
      - last:       nb d'événements sur 24h (tous types, inchangé)
      - visits_total, unique_ips, visits_per_user, attempts_total, attempts_success
    """
    days = max(1, min(days, 365))
    out = {
        "days": days,
        "labels": [],
        "visits_by_day": [],
        "sims_by_day": [],
        "by_event": [],
        "last": 0,
        "visits_total": 0,
        "unique_ips": 0,
        "visits_per_user": 0.0,
        "attempts_total": 0,
        "attempts_success": 0,
    }

    def _query(conn: psycopg.Connection):
        # Série complète de dates (inclure les jours sans donnée)
        # borne basse = jour 0h UTC il y a "days-1" jours
        series_sql = """
        WITH bounds AS (
          SELECT
            date_trunc('day', (NOW() AT TIME ZONE 'UTC'))::timestamp AS today_utc,
            date_trunc('day', (NOW() AT TIME ZONE 'UTC') - %s::interval)::timestamp AS start_utc
        ),
        series AS (
          SELECT generate_series(b.start_utc, b.today_utc, '1 day'::interval) AS d
          FROM bounds b
        ),
        visits AS (
          SELECT date_trunc('day', ts_utc)::timestamp AS d, COUNT(*) AS n
          FROM events
          WHERE ts_utc >= (NOW() AT TIME ZONE 'UTC') - %s::interval
            AND event = 'pageview'
          GROUP BY 1
        ),
        sims AS (
          SELECT date_trunc('day', ts_utc)::timestamp AS d, COUNT(*) AS n
          FROM events
          WHERE ts_utc >= (NOW() AT TIME ZONE 'UTC') - %s::interval
            AND event = 'calculate_click'
          GROUP BY 1
        )
        SELECT
          to_char(s.d, 'YYYY-MM-DD') AS day,
          COALESCE(v.n, 0) AS visits,
          COALESCE(c.n, 0) AS sims
        FROM series s
        LEFT JOIN visits v ON v.d = s.d
        LEFT JOIN sims   c ON c.d = s.d
        ORDER BY s.d
        """
        by_day = conn.execute(series_sql, (f"{days} days", f"{days} days", f"{days} days")).fetchall()

        by_event = conn.execute(
            """
            SELECT event, COUNT(*) AS n
            FROM events
            WHERE ts_utc >= (NOW() AT TIME ZONE 'UTC') - %s::interval
            GROUP BY 1 ORDER BY 2 DESC, 1 ASC
            """,
            (f"{days} days",),
        ).fetchall()

        last24 = conn.execute(
            "SELECT COUNT(*) AS cnt FROM events WHERE ts_utc >= (NOW() AT TIME ZONE 'UTC') - INTERVAL '24 hours'"
        ).fetchone()["cnt"]

        visits_total = conn.execute(
            "SELECT COUNT(*) AS cnt FROM events WHERE ts_utc >= (NOW() AT TIME ZONE 'UTC') - %s::interval AND event='pageview'",
            (f"{days} days",),
        ).fetchone()["cnt"]

        attempts_total = conn.execute(
            "SELECT COUNT(*) AS cnt FROM events WHERE ts_utc >= (NOW() AT TIME ZONE 'UTC') - %s::interval AND event='calculate_click'",
            (f"{days} days",),
        ).fetchone()["cnt"]

        attempts_success = conn.execute(
            "SELECT COUNT(*) AS cnt FROM events WHERE ts_utc >= (NOW() AT TIME ZONE 'UTC') - %s::interval AND event='calculate_success'",
            (f"{days} days",),
        ).fetchone()["cnt"]

        unique_ips = conn.execute(
            "SELECT COUNT(DISTINCT ip) AS cnt FROM events WHERE ts_utc >= (NOW() AT TIME ZONE 'UTC') - %s::interval",
            (f"{days} days",),
        ).fetchone()["cnt"]

        return by_day, by_event, last24, visits_total, attempts_total, attempts_success, unique_ips

    res = _with_db(_query) or ([], [], 0, 0, 0, 0, 0)
    rows_day, rows_event, last24, visits_total, attempts_total, attempts_success, uips = res

    out["labels"] = [r["day"] for r in rows_day]
    out["visits_by_day"] = [int(r["visits"]) for r in rows_day]
    out["sims_by_day"]   = [int(r["sims"]) for r in rows_day]
    out["by_event"] = [{"event": r["event"], "n": int(r["n"])} for r in rows_event]
    out["last"] = int(last24 or 0)
    out["visits_total"] = int(visits_total or 0)
    out["attempts_total"] = int(attempts_total or 0)
    out["attempts_success"] = int(attempts_success or 0)
    out["unique_ips"] = int(uips or 0)
    out["visits_per_user"] = (out["visits_total"] / out["unique_ips"]) if out["unique_ips"] else 0.0

    return JSONResponse(out)

# -------------------- Nouveau dashboard /stats.html --------------------
@app.get("/stats.html", include_in_schema=False)
def stats_html():
    html = """
<!doctype html><html lang="fr"><meta charset="utf-8">
<title>Spirit AM - Tracker de la rente obligataire</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Cache-Control" content="no-store" />
<style>
  :root{
    color-scheme: light dark;
    --bg:#0b1220; --bg2:#0f172a; --card:#0f1b31; --edge:#213655; --text:#e6eefc; --muted:#94a3b8; --brand:#1d5fd3;
  }
  @media (prefers-color-scheme: light){
    :root{ --bg:#f6f8fc; --bg2:#eef2f8; --card:#ffffff; --edge:#d6e0ee; --text:#0f172a; --muted:#67758a; --brand:#1d5fd3; }
  }
  *{box-sizing:border-box}
  body{margin:0;font-family:Inter,system-ui,-apple-system,Segoe UI,Roboto,Arial;color:var(--text);background:linear-gradient(180deg,var(--bg),var(--bg2));}
  .wrap{max-width:1180px;margin:28px auto;padding:0 18px}
  header{display:flex;justify-content:space-between;align-items:center;background:linear-gradient(90deg,var(--brand),#0ea5e9);
         color:#fff;border-radius:16px;padding:16px 18px;box-shadow:0 12px 32px rgba(0,0,0,.25)}
  h1{margin:0;font-size:20px;font-weight:800}
  .toolbar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:16px 0}
  .seg{display:flex;background:var(--card);border:1px solid var(--edge);border-radius:12px;overflow:hidden}
  .seg button{appearance:none;border:0;background:transparent;padding:9px 14px;font-weight:600;color:var(--muted);cursor:pointer}
  .seg button.active{color:var(--text);background:rgba(29,95,211,.12)}
  .btn, a.btn{padding:9px 12px;border-radius:10px;border:1px solid var(--edge);background:var(--card);color:var(--text);text-decoration:none}
  .grid{display:grid;grid-template-columns:1.2fr 1.8fr;gap:16px}
  @media (max-width:980px){ .grid{grid-template-columns:1fr} }
  .card{background:var(--card);border:1px solid var(--edge);border-radius:14px;padding:16px;box-shadow:0 8px 28px rgba(0,0,0,.16)}
  .kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}
  .kpi{background:linear-gradient(180deg,rgba(29,95,211,.08),transparent);border:1px solid var(--edge);border-radius:12px;padding:16px}
  .kpi .label{color:var(--muted);font-size:12px}
  .kpi .val{font-size:28px;font-weight:800;letter-spacing:.2px;transition:transform .2s ease}
  .chart-box{height:360px;position:relative}
  .chart-box canvas{position:absolute;inset:0;width:100% !important;height:100% !important}
  .table-wrap{max-height:440px;overflow:auto;border:1px solid var(--edge);border-radius:12px}
  table{width:100%;border-collapse:collapse;table-layout:fixed}
  th,td{padding:10px;border-bottom:1px solid var(--edge);text-align:left;font-size:14px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  th{position:sticky;top:0;background:var(--card);z-index:1}
  .muted{color:var(--muted)} .status{margin-left:auto;font-size:13px;color:var(--muted)}
</style>

<div class="wrap">
  <header>
    <div><h1>Spirit AM - Tracker de la rente obligataire</h1></div>
    <a class="btn" href="/events.csv" target="_blank">Télécharger le CSV</a>
  </header>

  <div class="toolbar">
    <div class="seg" role="tablist" aria-label="Période">
      <button id="d7"  class="active" role="tab" aria-selected="true">7 jours</button>
      <button id="d30" role="tab">30 jours</button>
      <button id="d90" role="tab">90 jours</button>
    </div>
    <span id="status" class="status"></span>
  </div>

  <div class="kpis">
    <div class="kpi"><div class="label">Visites totales</div><div id="k_visits" class="val">–</div></div>
    <div class="kpi"><div class="label">Visites / utilisateur unique</div><div id="k_vpu" class="val">–</div></div>
    <div class="kpi"><div class="label">Tentatives totales</div><div id="k_attempts" class="val">–</div></div>
    <div class="kpi"><div class="label">Tentatives réussies</div><div id="k_success" class="val">–</div></div>
  </div>

  <div class="grid" style="margin-top:14px">
    <div class="card">
      <div class="muted" style="margin-bottom:8px">Courbes : visites vs. simulations</div>
      <div class="chart-box"><canvas id="chart"></canvas></div>
    </div>
    <div class="card">
      <div class="muted" style="margin-bottom:8px">Par type d’événement</div>
      <div class="table-wrap">
        <table>
          <thead><tr><th>Événement</th><th>Compteur</th></tr></thead>
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
  const kVisits = document.getElementById('k_visits');
  const kVPU    = document.getElementById('k_vpu');
  const kAtt    = document.getElementById('k_attempts');
  const kSucc   = document.getElementById('k_success');
  const byEventEl = document.getElementById('byEvent');
  const periodEl  = document.getElementById('period');
  let chart, currentDays = 7;

  const btns = {7:document.getElementById('d7'), 30:document.getElementById('d30'), 90:document.getElementById('d90')};
  Object.entries(btns).forEach(([d,el])=>{
    el.addEventListener('click', ()=>{ setActive(+d); load(); });
  });

  function setActive(days){
    currentDays = days;
    Object.values(btns).forEach(b=>b.classList.remove('active'));
    btns[days].classList.add('active');
  }
  function nb(x){ return new Intl.NumberFormat('fr-FR').format(x||0); }
  function nb2(x){ return new Intl.NumberFormat('fr-FR', { maximumFractionDigits: 2 }).format(x||0); }
  function setStatus(t){ statusEl.textContent = t || ""; }

  async function load(){
    setStatus("Chargement…");
    try{
      const r = await fetch('/stats?days=' + currentDays + '&_=' + Date.now(), { cache:'no-store' });
      const data = await r.json();

      // KPIs
      kVisits.textContent = nb(data.visits_total);
      kVPU.textContent    = data.unique_ips ? nb2(data.visits_per_user) : "–";
      kAtt.textContent    = nb(data.attempts_total);
      kSucc.textContent   = nb(data.attempts_success);
      [kVisits,kVPU,kAtt,kSucc].forEach(el=>{ el.style.transform='scale(1.06)'; setTimeout(()=>el.style.transform='',120); });

      // Période lisible
      if ((data.labels||[]).length){
        const a = data.labels[0], b = data.labels[data.labels.length-1];
        periodEl.textContent = `Période affichée : ${a} → ${b}`;
      } else { periodEl.textContent = "Aucune donnée sur la période."; }

      // Chart (2 courbes + légende)
      const labels = data.labels || [];
      const visits = data.visits_by_day || [];
      const sims   = data.sims_by_day || [];
      if (chart) chart.destroy();
      chart = new Chart(document.getElementById('chart'), {
        type: 'line',
        data: {
          labels,
          datasets:[
            { label:'Visites', data: visits, tension:.25, fill:false },
            { label:'Simulations', data: sims, tension:.25, fill:false }
          ]
        },
        options: {
          responsive:true, maintainAspectRatio:false,
          plugins:{ legend:{ display:true, position:'top' }, tooltip:{ mode:'index', intersect:false } },
          interaction:{ mode:'index', intersect:false },
          scales:{ y:{ beginAtZero:true, ticks:{ precision:0 } } }
        }
      });

      // Tableau by_event (inchangé)
      byEventEl.innerHTML = (data.by_event||[]).map(x =>
        `<tr><td title="${x.event||''}">${x.event||''}</td><td>${nb(x.n)}</td></tr>`
      ).join('') || `<tr><td colspan="2" class="muted">Aucune donnée.</td></tr>`;

      setStatus("");
    }catch(e){
      console.error(e);
      setStatus("Erreur de chargement");
      byEventEl.innerHTML = `<tr><td colspan="2" style="color:#f43f5e">Impossible de charger les données.</td></tr>`;
    }
  }

  setActive(7);
  load();
</script>
"""
    return HTMLResponse(textwrap.dedent(html), headers={"Cache-Control": "no-store"})

# -------------------- Startup --------------------
@app.on_event("startup")
def on_startup():
    _get_pool()
