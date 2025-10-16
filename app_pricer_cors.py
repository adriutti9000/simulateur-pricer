from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from pricer_engine import compute_annuity
from datetime import datetime, timedelta
import psycopg
import os

app = FastAPI()

# --- CORS setup ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Database (Neon) ---
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://neondb_owner:YOUR_PASSWORD@YOUR_CLUSTER.neon.tech/neondb?sslmode=require"
)

def init_db():
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS events (
                        id SERIAL PRIMARY KEY,
                        timestamp TIMESTAMP DEFAULT NOW(),
                        event_type TEXT,
                        ip TEXT
                    );
                """)
                conn.commit()
        print("Postgres: CONNECTED & TABLE READY")
    except Exception as e:
        print("Database init failed:", e)

init_db()

# --- ROUTES ---

@app.get("/")
def root():
    return {"message": "API du simulateur de rente est en ligne ✅"}


# ---------------- COMPUTE ----------------
@app.post("/compute")
async def compute(data: dict):
    try:
        result = compute_annuity(data)
        return JSONResponse({"result": result})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ---------------- TRACKING ----------------
@app.post("/collect")
async def collect_event(request: Request):
    data = await request.json()
    event_type = data.get("event_type", "unknown")
    ip = request.client.host
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO events (event_type, ip) VALUES (%s, %s)", (event_type, ip))
                conn.commit()
        return {"status": "ok"}
    except Exception as e:
        return {"error": str(e)}


@app.get("/stats")
def get_stats(days: int = 30):
    try:
        since = datetime.now() - timedelta(days=days)
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT event_type, DATE(timestamp)
                    FROM events
                    WHERE timestamp > %s
                    ORDER BY timestamp ASC
                """, (since,))
                rows = cur.fetchall()

        daily = {}
        for event, date in rows:
            date_str = date.strftime("%Y-%m-%d")
            if date_str not in daily:
                daily[date_str] = {"pageviews": 0, "clicks": 0, "success": 0, "errors": 0}
            if event == "pageview":
                daily[date_str]["pageviews"] += 1
            elif event == "calculate_click":
                daily[date_str]["clicks"] += 1
            elif event == "calculate_success":
                daily[date_str]["success"] += 1
            elif event == "calculate_error":
                daily[date_str]["errors"] += 1

        total = {"pageviews": 0, "clicks": 0, "success": 0, "errors": 0}
        for d in daily.values():
            for k in total:
                total[k] += d[k]

        return {
            **total,
            "daily": [
                {"date": d, **daily[d]} for d in sorted(daily.keys())
            ]
        }

    except Exception as e:
        return {"error": str(e)}


@app.get("/events.csv")
def export_csv():
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id, timestamp, event_type, ip FROM events ORDER BY timestamp DESC")
                rows = cur.fetchall()

        csv_content = "id,timestamp,event_type,ip\n"
        for r in rows:
            csv_content += f"{r[0]},{r[1]},{r[2]},{r[3]}\n"

        return FileResponse(
            path=None,
            media_type="text/csv",
            filename="events.csv",
            headers={"Content-Disposition": "inline"},
            content=csv_content.encode("utf-8"),
        )

    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ---------------- SERVE STATS.HTML ----------------
@app.get("/stats.html")
async def serve_stats_html():
    file_path = os.path.join(os.path.dirname(__file__), "stats.html")
    return FileResponse(file_path, media_type="text/html")
