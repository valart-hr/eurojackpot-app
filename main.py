from fastapi import FastAPI
from fastapi.responses import HTMLResponse
import requests
import psycopg2
from bs4 import BeautifulSoup
import os
import re
import datetime
import time
import threading
from itertools import combinations

app = FastAPI()

DATABASE_URL = os.getenv("DATABASE_URL")

UPDATE_INTERVAL_SECONDS = 60 * 60 * 24
START_YEAR = 2012
CURRENT_YEAR = datetime.date.today().year


def get_conn():
    return psycopg2.connect(DATABASE_URL)


def render_layout(title: str, body: str):
    return f"""
    <html>
    <head>
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <meta name="theme-color" content="#111827" />
        <title>{title}</title>
        <style>
            body {{
                margin: 0;
                font-family: Arial, sans-serif;
                background: #f3f4f6;
                color: #111827;
            }}
            .app {{
                max-width: 900px;
                margin: 0 auto;
                padding: 14px;
            }}
            .header {{
                background: linear-gradient(135deg, #111827, #1f2937);
                color: white;
                border-radius: 18px;
                padding: 18px;
                margin-bottom: 14px;
                box-shadow: 0 8px 24px rgba(0,0,0,0.18);
            }}
            .header h1 {{
                margin: 0 0 6px 0;
                font-size: 26px;
            }}
            .muted {{
                color: #9ca3af;
                font-size: 14px;
            }}
            .grid {{
                display: grid;
                grid-template-columns: repeat(2, 1fr);
                gap: 10px;
                margin-bottom: 14px;
            }}
            .stat {{
                background: white;
                border-radius: 16px;
                padding: 14px;
                box-shadow: 0 2px 10px rgba(0,0,0,0.08);
            }}
            .stat .label {{
                font-size: 13px;
                color: #6b7280;
                margin-bottom: 6px;
            }}
            .stat .value {{
                font-size: 22px;
                font-weight: 700;
            }}
            .nav {{
                display: flex;
                flex-wrap: wrap;
                gap: 8px;
                margin-bottom: 14px;
            }}
            .nav a {{
                display: inline-block;
                padding: 10px 14px;
                background: #111827;
                color: white;
                text-decoration: none;
                border-radius: 12px;
                font-size: 14px;
            }}
            .card {{
                background: white;
                border-radius: 16px;
                padding: 14px;
                margin-bottom: 12px;
                box-shadow: 0 2px 10px rgba(0,0,0,0.08);
            }}
            .title {{
                font-size: 18px;
                font-weight: 700;
                margin-bottom: 8px;
            }}
            .row {{
                margin: 4px 0;
                font-size: 15px;
            }}
            table {{
                width: 100%;
                border-collapse: collapse;
                background: white;
                border-radius: 16px;
                overflow: hidden;
                box-shadow: 0 2px 10px rgba(0,0,0,0.08);
            }}
            th, td {{
                padding: 10px;
                border-bottom: 1px solid #e5e7eb;
                text-align: left;
                font-size: 14px;
            }}
            th {{
                background: #111827;
                color: white;
                position: sticky;
                top: 0;
            }}
        </style>
    </head>
    <body>
        <div class="app">{body}</div>
    </body>
    </html>
    """


@app.get("/health")
def health():
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM eurojackpot_draws")
        count = cur.fetchone()[0]
        cur.close()
        conn.close()
        return {"ok": True, "draws_in_db": count, "status": "running"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def fetch_year_page(year: int) -> str:
    url = f"https://www.beatlottery.co.uk/eurojackpot/draw-history/year/{year}"
    r = requests.get(
        url,
        timeout=30,
        headers={"User-Agent": "Mozilla/5.0"}
    )
    r.raise_for_status()
    return r.text


def parse_draws_from_html(html: str):
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text("\n", strip=True)

    pattern = re.compile(
        r"(?m)^"
        r"(\d{1,2}\s+[A-Z][a-z]{2}\s+\d{4})"      # 31 Dec 2024
        r"\s+\d{2}/\d{2}/\d{4}\s+(?:Tue|Fri)\s*$" # 31/12/2024 Tue
        r"\n+"
        r"(\d{2})\s+(\d{2})\s+(\d{2})\s+(\d{2})\s+(\d{2})"
        r"\s+EURO NUMBERS\s+"
        r"(\d{2})\s+(\d{2})",
        re.MULTILINE,
    )

    draws = []

    for match in pattern.finditer(text):
        draw_date = datetime.datetime.strptime(match.group(1), "%d %b %Y").date()
        main_numbers = sorted([int(match.group(i)) for i in range(2, 7)])
        euro_numbers = sorted([int(match.group(i)) for i in range(7, 9)])

        draws.append({
            "draw_date": draw_date,
            "main_numbers": main_numbers,
            "euro_numbers": euro_numbers,
        })

    return draws

def upsert_draws(draws):
    conn = get_conn()
    cur = conn.cursor()
    processed = 0

    for draw in draws:
        n = draw["main_numbers"]
        e = draw["euro_numbers"]

        cur.execute(
            """
            INSERT INTO eurojackpot_draws
            (draw_date, n1, n2, n3, n4, n5, e1, e2, source)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (draw_date) DO UPDATE SET
                n1 = EXCLUDED.n1,
                n2 = EXCLUDED.n2,
                n3 = EXCLUDED.n3,
                n4 = EXCLUDED.n4,
                n5 = EXCLUDED.n5,
                e1 = EXCLUDED.e1,
                e2 = EXCLUDED.e2,
                source = EXCLUDED.source,
                scraped_at = NOW()
            """,
            (
                draw["draw_date"],
                n[0], n[1], n[2], n[3], n[4],
                e[0], e[1],
                "beatlottery_year_archive",
            ),
        )
        processed += 1

    conn.commit()
    cur.close()
    conn.close()
    return processed


def update_all_draws():
    print("Updating Eurojackpot history...")
    total = 0

    for year in range(START_YEAR, CURRENT_YEAR + 1):
        try:
            html = fetch_year_page(year)
            draws = parse_draws_from_html(html)
            count = upsert_draws(draws)
            total += count
            print(f"Year {year}: parsed {len(draws)}, processed {count}")
        except Exception as e:
            print(f"Year {year} error: {e}")

    print(f"Update complete. Rows processed: {total}")


@app.get("/update-now")
def update_now():
    try:
        update_all_draws()
        return {"status": "updated"}
    except Exception as e:
        return {"error": str(e)}


@app.get("/test-parse")
def test_parse():
    try:
        html = fetch_year_page(2024)
        draws = parse_draws_from_html(html)
        return {
            "count": len(draws),
            "first_5": draws[:5]
        }
    except Exception as e:
        return {"error": str(e)}


def get_all_draws():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT draw_date, n1, n2, n3, n4, n5, e1, e2
        FROM eurojackpot_draws
        ORDER BY draw_date ASC
    """)
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


def compute_stats():
    rows = get_all_draws()

    main_freq = {i: 0 for i in range(1, 51)}
    euro_freq = {i: 0 for i in range(1, 13)}
    pair_freq = {}

    last_seen_main = {i: None for i in range(1, 51)}
    last_seen_euro = {i: None for i in range(1, 13)}

    for idx, row in enumerate(rows):
        mains = [row[1], row[2], row[3], row[4], row[5]]
        euros = [row[6], row[7]]

        for n in mains:
            main_freq[n] += 1
            last_seen_main[n] = idx

        for e in euros:
            euro_freq[e] += 1
            last_seen_euro[e] = idx

        for pair in combinations(sorted(mains), 2):
            pair_freq[pair] = pair_freq.get(pair, 0) + 1

    total_draws = len(rows)

    overdue_main = []
    for n in range(1, 51):
        gap = total_draws if last_seen_main[n] is None else total_draws - 1 - last_seen_main[n]
        overdue_main.append((n, gap, main_freq[n]))

    overdue_euro = []
    for e in range(1, 13):
        gap = total_draws if last_seen_euro[e] is None else total_draws - 1 - last_seen_euro[e]
        overdue_euro.append((e, gap, euro_freq[e]))

    def freq_last(window):
        main = {i: 0 for i in range(1, 51)}
        euro = {i: 0 for i in range(1, 13)}
        subset = rows[-window:] if window <= len(rows) else rows

        for row in subset:
            mains = [row[1], row[2], row[3], row[4], row[5]]
            euros = [row[6], row[7]]

            for n in mains:
                main[n] += 1
            for e in euros:
                euro[e] += 1

        return main, euro, len(subset)

    main10, euro10, size10 = freq_last(10)
    main50, euro50, size50 = freq_last(50)

    top_pairs = sorted(pair_freq.items(), key=lambda x: (-x[1], x[0]))[:20]

    return {
        "total_draws": total_draws,
        "main_freq": main_freq,
        "euro_freq": euro_freq,
        "overdue_main": sorted(overdue_main, key=lambda x: (-x[1], x[0])),
        "overdue_euro": sorted(overdue_euro, key=lambda x: (-x[1], x[0])),
        "main10": main10,
        "euro10": euro10,
        "size10": size10,
        "main50": main50,
        "euro50": euro50,
        "size50": size50,
        "top_pairs": top_pairs,
    }
import random

@app.get("/", response_class=HTMLResponse)
def home():
    stats = compute_stats()

    body = f"""
    <div class="header">
        <h1>Eurojackpot Stats</h1>
        <div class="muted">Auto-update arhiva i statistika</div>
    </div>

    <div class="grid">
        <div class="stat"><div class="label">Ukupno izvlačenja</div><div class="value">{stats['total_draws']}</div></div>
        <div class="stat"><div class="label">Format</div><div class="value">5/50 + 2/12</div></div>
    </div>

    <div class="nav">
        <a href="/draws">Zadnja izvlačenja</a>
        <a href="/stats">Frekvencije</a>
        <a href="/overdue">Overdue</a>
        <a href="/hot-cold">Hot/Cold</a>
        <a href="/health">Health</a>
        <a href="/update-now">Update now</a>
    </div>

    <div class="card">
        <div class="title">Napomena</div>
        <div class="row">Ovo je statistički pregled. Overdue i hot/cold ne predviđaju sljedeće izvlačenje.</div>
    </div>
    """
    return render_layout("Eurojackpot Stats", body)


@app.get("/draws", response_class=HTMLResponse)
def draws_page():
    rows = get_all_draws()[-30:][::-1]
    trs = ""

    for row in rows:
        trs += f"""
        <tr>
            <td>{row[0]}</td>
            <td>{row[1]} {row[2]} {row[3]} {row[4]} {row[5]}</td>
            <td>{row[6]} {row[7]}</td>
        </tr>
        """

    body = f"""
    <div class="header"><h1>Zadnja izvlačenja</h1><div class="muted">Posljednjih 30 kola</div></div>
    <div class="nav"><a href="/">Početna</a></div>
    <table>
        <tr><th>Datum</th><th>Glavni brojevi</th><th>Euro brojevi</th></tr>
        {trs}
    </table>
    """
    return render_layout("Draws", body)


@app.get("/stats", response_class=HTMLResponse)
def stats_page():
    stats = compute_stats()

    top_main = sorted(stats["main_freq"].items(), key=lambda x: (-x[1], x[0]))[:15]
    top_euro = sorted(stats["euro_freq"].items(), key=lambda x: (-x[1], x[0]))[:12]

    main_rows = "".join([f"<tr><td>{n}</td><td>{c}</td></tr>" for n, c in top_main])
    euro_rows = "".join([f"<tr><td>{n}</td><td>{c}</td></tr>" for n, c in top_euro])

    body = f"""
    <div class="header"><h1>Frekvencije</h1><div class="muted">Najčešće izvučeni brojevi</div></div>
    <div class="nav"><a href="/">Početna</a><a href="/hot-cold">Hot/Cold</a></div>

    <div class="card"><div class="title">Top glavni brojevi 1–50</div></div>
    <table><tr><th>Broj</th><th>Pojavljivanja</th></tr>{main_rows}</table>

    <div style="height:12px;"></div>

    <div class="card"><div class="title">Top Euro brojevi 1–12</div></div>
    <table><tr><th>Broj</th><th>Pojavljivanja</th></tr>{euro_rows}</table>
    """
    return render_layout("Stats", body)


@app.get("/overdue", response_class=HTMLResponse)
def overdue_page():
    stats = compute_stats()

    main_rows = "".join([
        f"<tr><td>{n}</td><td>{gap}</td><td>{freq}</td></tr>"
        for n, gap, freq in stats["overdue_main"][:20]
    ])

    euro_rows = "".join([
        f"<tr><td>{n}</td><td>{gap}</td><td>{freq}</td></tr>"
        for n, gap, freq in stats["overdue_euro"][:12]
    ])

    body = f"""
    <div class="header"><h1>Overdue</h1><div class="muted">Koliko kola broj nije izašao</div></div>
    <div class="nav"><a href="/">Početna</a><a href="/stats">Frekvencije</a></div>

    <div class="card"><div class="title">Glavni brojevi</div></div>
    <table><tr><th>Broj</th><th>Gap (kola)</th><th>Ukupno izlazaka</th></tr>{main_rows}</table>

    <div style="height:12px;"></div>

    <div class="card"><div class="title">Euro brojevi</div></div>
    <table><tr><th>Broj</th><th>Gap (kola)</th><th>Ukupno izlazaka</th></tr>{euro_rows}</table>
    """
    return render_layout("Overdue", body)


@app.get("/hot-cold", response_class=HTMLResponse)
def hot_cold_page():
    stats = compute_stats()

    top10_main = sorted(stats["main10"].items(), key=lambda x: (-x[1], x[0]))[:15]
    cold10_main = sorted(stats["main10"].items(), key=lambda x: (x[1], x[0]))[:15]
    top50_main = sorted(stats["main50"].items(), key=lambda x: (-x[1], x[0]))[:15]

    pairs_rows = "".join([
        f"<tr><td>{a}-{b}</td><td>{count}</td></tr>"
        for (a, b), count in stats["top_pairs"]
    ])

    hot10_rows = "".join([f"<tr><td>{n}</td><td>{c}</td></tr>" for n, c in top10_main])
    cold10_rows = "".join([f"<tr><td>{n}</td><td>{c}</td></tr>" for n, c in cold10_main])
    hot50_rows = "".join([f"<tr><td>{n}</td><td>{c}</td></tr>" for n, c in top50_main])

    body = f"""
    <div class="header"><h1>Hot / Cold</h1><div class="muted">Zadnjih 10 i 50 izvlačenja</div></div>
    <div class="nav"><a href="/">Početna</a><a href="/overdue">Overdue</a></div>

    <div class="card"><div class="title">Hot glavni brojevi (zadnjih {stats['size10']})</div></div>
    <table><tr><th>Broj</th><th>Pojavljivanja</th></tr>{hot10_rows}</table>

    <div style="height:12px;"></div>

    <div class="card"><div class="title">Cold glavni brojevi (zadnjih {stats['size10']})</div></div>
    <table><tr><th>Broj</th><th>Pojavljivanja</th></tr>{cold10_rows}</table>

    <div style="height:12px;"></div>

    <div class="card"><div class="title">Hot glavni brojevi (zadnjih {stats['size50']})</div></div>
    <table><tr><th>Broj</th><th>Pojavljivanja</th></tr>{hot50_rows}</table>

    <div style="height:12px;"></div>

    <div class="card"><div class="title">Najčešći parovi glavnih brojeva</div></div>
    <table><tr><th>Par</th><th>Pojavljivanja</th></tr>{pairs_rows}</table>
    """
    return render_layout("Hot/Cold", body)


def worker_loop():
    while True:
        try:
            update_all_draws()
        except Exception as e:
            print("Update error:", e)

        time.sleep(UPDATE_INTERVAL_SECONDS)


@app.on_event("startup")
def startup():
    thread = threading.Thread(target=worker_loop, daemon=True)
    thread.start()
    def build_predictions():
    stats = compute_stats()

    overdue_main = [x[0] for x in stats["overdue_main"][:15]]
    hot_main = [x[0] for x in sorted(stats["main_freq"].items(), key=lambda x: (-x[1], x[0]))[:15]]

    medium_main_candidates = [
        x[0] for x in sorted(
            stats["main_freq"].items(),
            key=lambda x: x[1]
        )[15:35]
    ]

    overdue_euro = [x[0] for x in stats["overdue_euro"][:6]]
    hot_euro = [x[0] for x in sorted(stats["euro_freq"].items(), key=lambda x: (-x[1], x[0]))[:6]]

    tickets = []
    seen = set()

    attempts = 0
    while len(tickets) < 5 and attempts < 100:
        attempts += 1

        main_numbers = set()
        euro_numbers = set()

        # 2 overdue glavna
        while len(main_numbers) < 2:
            main_numbers.add(random.choice(overdue_main))

        # 2 hot glavna
        hot_pool = [n for n in hot_main if n not in main_numbers]
        while len(main_numbers) < 4 and hot_pool:
            choice = random.choice(hot_pool)
            main_numbers.add(choice)
            hot_pool = [n for n in hot_pool if n != choice]

        # 1 srednji/random glavni
        middle_pool = [n for n in medium_main_candidates if n not in main_numbers]
        if middle_pool:
            main_numbers.add(random.choice(middle_pool))

        # ako slučajno nema 5, dopuni iz cijelog skupa
        all_main = list(range(1, 51))
        while len(main_numbers) < 5:
            main_numbers.add(random.choice(all_main))

        # euro: 1 overdue + 1 hot
        euro_numbers.add(random.choice(overdue_euro))
        hot_euro_pool = [n for n in hot_euro if n not in euro_numbers]
        if hot_euro_pool:
            euro_numbers.add(random.choice(hot_euro_pool))

        while len(euro_numbers) < 2:
            euro_numbers.add(random.randint(1, 12))

        main_sorted = tuple(sorted(main_numbers))
        euro_sorted = tuple(sorted(euro_numbers))

        key = (main_sorted, euro_sorted)
        if key in seen:
            continue

        seen.add(key)
        tickets.append({
            "main_numbers": list(main_sorted),
            "euro_numbers": list(euro_sorted),
            "profile": {
                "main": "2 overdue + 2 hot + 1 middle",
                "euro": "1 overdue + 1 hot"
            }
        })

    return tickets
