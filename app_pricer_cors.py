# app_pricer_cors.py
# ---------------------------------------------------------
# API FastAPI + tracking PostgreSQL (Neon) + dashboard /stats.html intégré
# Dépendances (requirements.txt) :
#   fastapi
#   uvicorn[standard]
#   pydantic>=2
#   psycopg[binary,pool]==3.2.10
# ---------------------------------------------------------

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
    # keepalives pour limiter les coupures d'inactivité
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
    """Retourne un pool prêt. Recrée si besoin et initialise la table/colonnes."""
    global _pool
    if _pool is None:
        try:
            _pool = _build_pool()
            if _pool:
                with _pool.connection() as conn:
                    # Table minimale
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
                    # 🔧 Migrations "douces" : on ajoute la colonne ref si elle n'existe pas
                    conn.execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS ref TEXT;")
                print("Postgres: CONNECTED & TABLE READY (migrée)")
        except Exception as e:
            print("Postgres: INIT ERROR", e)
            _pool = None
    return _pool

def _with_db(action: Callable[[psycopg.Connection], Any]) -> Any:
    """
    Exécute une action DB avec retries si la connexion est fermée.
    (Pas de paramètre 'pool' dans les routes -> pas d'erreur FastAPI.)
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
            # reset le pool et retente
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

# -------------------- Dashboard intégré /stats.html (design propre) --------------------
@app.get("/stats.html", include_in_schema=False)
def stats_html():
    html = """
    <!doctype html><html lang="fr"><meta charset="utf-8">
    <title>Stats simulateur</title>
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <style>
      :root{
        --bg:#f6f8fb; --card:#fff; --text:#0f172a; --muted:#64748b; --border:#e6eaf2;
        --primary:#1d5fd3; --primary-h:#184fb0;
      }
      *{box-sizing:border-box}
      body{margin:0; font-family:system-ui, -apple-system, Segoe UI, Roboto, Arial; color:var(--text); background:var(--bg);}
      .wrap{max-width:1100px; margin:28px auto; padding:0 16px;}
      header{display:flex; justify-content:space-between; align-items:center; margin-bottom:14px}
      h1{margin:0; font-size:22px}
      .toolbar{display:flex; gap:10px; align-items:center; flex-wrap:wrap; margin:10px 0 18px}
      select,button,a{padding:8px 12px; border-radius:10px; border:1px solid var(--border); font-size:14px; background:#fff; color:var(--text); text-decoration:none}
      button{background:var(--primary); color:#fff; border:none}
      button:hover{background:var(--primary-h)}
      .grid{display:grid; grid-template-columns:1fr 2fr; gap:16px}
      @media (max-width:900px){ .grid{grid-template-columns:1fr} }
      .card{background:var(--card); border:1px solid var(--border); border-radius:14px; padding:16px; box-shadow:0 4px 10px rgba(0,0,0,.04)}
      .kpi{font-size:34px; font-weight:800}
      .muted{color:var(--muted)}
      /* ---- Chart ---- */
      .chart-box{height:320px; position:relative}
      .chart-box canvas{position:absolute; inset:0; width:100% !important; height:100% !important}
      /* ---- Table stable ---- */
      table{width:100%; border-collapse:collapse; table-layout:fixed}
      th, td{padding:10px; border-bottom:1px solid var(--border); text-align:left; font-size:14px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis}
      th{position:sticky; top:0; background:#fff; z-index:1}
      .table-wrap{max-height:420px; overflow:auto; border:1px solid var(--border); border-radius:12px}
      .cols-2{width:40%} .cols-1{width:60%}
      /* ---- Badges ---- */
      .badge{display:inline-block; padding:4px 8px; border-radius:999px; background:#eef4ff; color:#1d5fd3; font-size:12px; margin-left:8px}
      .info{margin-top:10px; font-size:13px; color:var(--muted)}
      .status{margin-left:auto; font-size:13px; color:var(--muted)}
    </style>

    <div class="wrap">
      <header>
        <h1>Statistiques d’usage <span class="badge">Live</span></h1>
        <a href="/events.csv" target="_blank">Télécharger le CSV</a>
      </header>

      <div class="toolbar">
        <span class="muted">Période :</span>
        <select id="days">
          <option value="7">7 jours</option>
          <option value="30" selected>30 jours</option>
          <option value="90">90 jours</option>
        </select>
        <button id="refresh">Actualiser</button>
        <span id="status" class="status"></span>
      </div>

      <div class="grid">
        <div class="card">
          <div class="muted">Événements sur 24h</div>
          <div id="kpi" class="kpi">–</div>
          <div id="kpi-sub" class="info"></div>
        </div>

        <div class="card">
          <div class="muted" style="margin-bottom:8px">Événements par jour</div>
          <div class="chart-box"><canvas id="chart"></canvas></div>
        </div>
      </div>

      <div class="card" style="margin-top:16px">
        <div class="muted" style="margin-bottom:8px">Par type d’événement</div>
        <div class="table-wrap">
          <table>
            <thead>
              <tr><th class="cols-1">Événement</th><th class="cols-2">Compteur</th></tr>
            </thead>
            <tbody id="byEvent"></tbody>
          </table>
        </div>
        <div id="totals" class="info"></div>
      </div>
    </div>

    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <script>
      const statusEl = document.getElementById('status');
      const kpiEl = document.getElementById('kpi');
      const kpiSubEl = document.getElementById('kpi-sub');
      const byEventEl = document.getElementById('byEvent');
      const totalsEl = document.getElementById('totals');
      const daysSel = document.getElementById('days');
      let chart;

      function setStatus(t){ statusEl.textContent = t || ""; }

      async function load(){
        setStatus("Chargement…");
        const days = daysSel.value;
        try{
          const r = await fetch('/stats?days=' + days, {cache:'no-store'});
          const data = await r.json();

          // KPI 24h
          const last = Number(data.last || 0);
          kpiEl.textContent = new Intl.NumberFormat('fr-FR').format(last);
          kpiSubEl.textContent = (data.by_day && data.by_day.length)
              ? `Période : ${data.by_day[0].day} → ${data.by_day[data.by_day.length-1].day}`
              : "";

          // Courbe
          const labels = (data.by_day || []).map(x => x.day);
          const values = (data.by_day || []).map(x => x.n);
          if(chart) chart.destroy();
          chart = new Chart(document.getElementById('chart'), {
            type: 'line',
            data: { labels, datasets: [{ label:'Événements', data: values, tension:.25 }] },
            options: {
              responsive:true, maintainAspectRatio:false,
              plugins:{ legend:{ display:false } },
              scales:{ y:{ beginAtZero:true, ticks:{ precision:0 } } }
            }
          });

          // Tableau stable
          const rows = (data.by_event || []).map(x => {
            const ev = String(x.event || '');
            const n  = Number(x.n || 0);
            return `<tr><td title="${ev}">${ev}</td><td>${new Intl.NumberFormat('fr-FR').format(n)}</td></tr>`;
          }).join('');
          byEventEl.innerHTML = rows || `<tr><td colspan="2" class="muted">Aucune donnée sur la période.</td></tr>`;

          // Totaux période
          const total = (data.by_event || []).reduce((s, x) => s + (Number(x.n)||0), 0);
          totalsEl.textContent = `Total événements sur ${days} jours : ${new Intl.NumberFormat('fr-FR').format(total)}`;

          setStatus("");
        }catch(e){
          setStatus("Erreur de chargement");
          byEventEl.innerHTML = `<tr><td colspan="2" style="color:#b91c1c">Impossible de charger les données.</td></tr>`;
        }
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
