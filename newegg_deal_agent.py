"""
Newegg deal pipeline (feed edition):
  Finder -> Evaluator (Gemini) -> Price Watcher -> Presenter (email)

Newegg blocks scripts (403), so the Finder reads public deal feeds (Reddit's
r/buildapcsales and deal sites) and keeps only the deals that mention Newegg.

  score >= MIN_SCORE         -> emailed right away
  WATCH_MIN_SCORE..MIN_SCORE -> put on the watchlist; emailed only if the same
                                product shows up again at a lower price
  below WATCH_MIN_SCORE      -> ignored

Setup (Windows PowerShell):
  py -m pip install requests google-genai
  $env:GEMINI_API_KEY = "..."           # free key from Google AI Studio
  $env:GEMINI_MODEL = "gemini-3.5-flash-lite"   # optional, this is the default
Email (optional - without it, results are printed on screen instead):
  $env:SMTP_USER = "you@gmail.com"      # Gmail needs an "app password"
  $env:SMTP_PASS = "your-app-password"
  $env:MAIL_TO = "you@gmail.com"
"""
import os, re, json, time, sqlite3, smtplib, html, difflib
import xml.etree.ElementTree as ET
from email.message import EmailMessage

import requests
from google import genai
from google.genai import types

# ---------- YOUR SETTINGS ----------
FEEDS = [
    "https://www.reddit.com/r/buildapcsales/new/.rss",
    "https://www.dealnews.com/c39/Computers/?rss=1",
    "https://slickdeals.net/newsearch.php?mode=frontpage&searcharea=deals&searchin=first&rss=1",
]
STORE = "newegg"       # keep only deals mentioning this word ("" = keep every store)
CRITERIA = """
- Real discount vs. the product's typical street price matters more than a 'sale' label.
- Prefer well-known brands and reputable sellers.
- Penalize unknown brands, refurbished or open-box items, and bundles that pad the price.
- Ignore accessories and items that look like price-padding.
"""
MIN_SCORE = 7          # 7+ : email right away
WATCH_MIN_SCORE = 5    # 5-6: watch for the same product at a lower price
DROP_PCT = 10          # alert when a watched product reappears this % cheaper
WATCH_DAYS = 14        # stop watching an item after this many days
MAX_PER_RUN = 25       # cap items scored per run (free-tier friendly)
PAUSE = 7              # seconds between AI calls (free tier ~10 per minute)
# Models are tried in order. Set GEMINI_MODEL to use your own pick.
MODELS = [m for m in (os.getenv("GEMINI_MODEL"), "gemini-3.5-flash-lite", "gemini-3.5-flash") if m]
DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "deals.db")
HEADERS = {"User-Agent": "newegg-deal-agent/1.0 (personal use)"}


def db():
    con = sqlite3.connect(DB)
    con.execute("CREATE TABLE IF NOT EXISTS seen (key TEXT PRIMARY KEY)")
    con.execute("""CREATE TABLE IF NOT EXISTS watchlist (
        key TEXT PRIMARY KEY, title TEXT, url TEXT, start_price REAL,
        score INTEGER, added TEXT DEFAULT CURRENT_TIMESTAMP,
        status TEXT DEFAULT 'watching')""")
    return con


# ---------- AGENT 1: FINDER (reads feeds) ----------
def local(tag):
    return tag.split("}")[-1]


def parse_feed(xml_text):
    """Reads both Atom (Reddit) and RSS (deal sites) feeds."""
    out = []
    root = ET.fromstring(xml_text)
    for el in root.iter():
        if local(el.tag) not in ("entry", "item"):
            continue
        f = {}
        for ch in el:
            n = local(ch.tag)
            if n == "link":
                if ch.get("href"):
                    f.setdefault("link", ch.get("href"))
                elif ch.text:
                    f.setdefault("link", ch.text.strip())
            elif n in ("title", "content", "description", "summary") and ch.text:
                f.setdefault(n, ch.text)
        body = html.unescape(f.get("content") or f.get("description") or f.get("summary") or "")
        title = html.unescape(f.get("title", "")).strip()
        link = f.get("link", "")
        store_url = re.search(r"https?://[^\s\"'<>]*newegg\.com[^\s\"'<>]*", body)
        price = re.search(r"\$\s?([\d,]+(?:\.\d{1,2})?)", title)
        out.append({
            "id": link or title,
            "title": title,
            "url": html.unescape(store_url.group()) if store_url else link,
            "price": float(price.group(1).replace(",", "")) if price else None,
            "snippet": re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body)).strip()[:300],
            "_text": (title + " " + body + " " + link).lower(),
        })
    return out


def find_offers():
    offers, seen_ids = [], set()
    for url in FEEDS:
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            r.raise_for_status()
            items = parse_feed(r.text)
        except Exception as e:
            print(f"[finder] failed {url}: {e}")
            continue
        kept = 0
        for it in items:
            if STORE and STORE not in it.pop("_text"):
                continue
            it.pop("_text", None)
            if it["id"] in seen_ids:
                continue
            seen_ids.add(it["id"])
            offers.append(it)
            kept += 1
        print(f"[finder] {url.split('/')[2]}: {len(items)} items, {kept} match '{STORE}'")
    return offers


def only_new(offers):
    """Skip anything already seen in an earlier run."""
    con = db()
    fresh = []
    for o in offers:
        if con.execute("SELECT 1 FROM seen WHERE key=?", (o["id"],)).fetchone():
            continue
        con.execute("INSERT INTO seen VALUES (?)", (o["id"],))
        fresh.append(o)
    con.commit()
    con.close()
    return fresh


# ---------- AGENT 2: PRICE WATCHER ----------
def norm(title):
    """Product name without [tags], prices and store names, for matching reposts."""
    t = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", title.lower())
    t = re.sub(r"\$\s?[\d,.]+|@\s*\w+|\bnewegg\b|\bfree shipping\b", " ", t)
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


def check_watchlist(new):
    """Pull out reposts of watched products. Returns (still_unscored, drops)."""
    con = db()
    con.execute("UPDATE watchlist SET status='expired' WHERE status='watching' "
                "AND added < datetime('now', ?)", (f"-{WATCH_DAYS} days",))
    rows = con.execute("SELECT key, title, start_price FROM watchlist "
                       "WHERE status='watching'").fetchall()
    remaining, drops = [], []
    for o in new:
        match = None
        if o["price"]:
            k = norm(o["title"])
            for key, title, start in rows:
                if difflib.SequenceMatcher(None, k, key).ratio() >= 0.85:
                    match = (key, title, start)
                    break
        if not match:
            remaining.append(o)
            continue
        key, title, start = match
        if o["price"] <= start * (1 - DROP_PCT / 100):
            drops.append({"title": o["title"], "url": o["url"], "start_price": start,
                          "price": o["price"], "drop": (1 - o["price"] / start) * 100})
            con.execute("UPDATE watchlist SET status='alerted' WHERE key=?", (key,))
    con.commit()
    con.close()
    print(f"[watcher] watching {len(rows)}, {len(drops)} price drops")
    return remaining, drops


def add_to_watchlist(offers):
    con = db()
    n = 0
    for o in offers:
        if WATCH_MIN_SCORE <= o.get("score", 0) < MIN_SCORE and o.get("price"):
            cur = con.execute(
                "INSERT OR IGNORE INTO watchlist (key, title, url, start_price, score) "
                "VALUES (?,?,?,?,?)", (norm(o["title"]), o["title"], o["url"], o["price"], o["score"]))
            n += cur.rowcount
    con.commit()
    con.close()
    print(f"[watcher] {n} new items on the watchlist")


# ---------- AGENT 3: EVALUATOR (Gemini, free tier) ----------
def make_client():
    try:
        return genai.Client(http_options=types.HttpOptions(timeout=60000))  # 60 s limit
    except Exception:
        return genai.Client()


def ask_gemini(client, prompt):
    last = None
    for model in MODELS:
        for attempt in range(3):
            try:
                resp = client.models.generate_content(
                    model=model, contents=prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json", temperature=0.2))
                return resp.text
            except Exception as e:
                last = e
                if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
                    time.sleep(30 * (attempt + 1))   # rate limit: wait, then retry
                    continue
                break                                 # other error: try the next model
    raise last


def evaluate(offers):
    client = make_client()
    results = []
    scorable = [o for o in offers if o["price"]][:MAX_PER_RUN]   # no price = can't judge
    for o in scorable:
        prompt = f"""You are a skeptical PC-parts deal evaluator. Score this deal from 1-10.
7+ = buy now, 5-6 = decent but could be cheaper, below 5 = skip.
You cannot browse the web. Judge the price against what you know of this product's
typical street price. If you do not recognize the product or are unsure, score at most 6
and say so in the reason.

Criteria:
{CRITERIA}

Deal:
Title: {o['title']}
Price: ${o['price']}
Details: {o['snippet']}

Reply with ONLY JSON: {{"score": int, "verdict": "buy"|"maybe"|"skip",
"reason": "<= 2 sentences", "red_flags": ["..."]}}"""
        try:
            text = ask_gemini(client, prompt).strip()
            text = re.sub(r"^```(?:json)?|```$", "", text).strip()
            o.update(json.loads(text))
            results.append(o)
        except Exception as e:
            print(f"[evaluator] skipped one item: {e}")
        time.sleep(PAUSE)
    print(f"[evaluator] scored {len(results)} of {len(offers)} new deals")
    return sorted(results, key=lambda x: x.get("score", 0), reverse=True)


# ---------- AGENT 4: PRESENTER ----------
def present(offers, drops):
    good = [o for o in offers if o.get("score", 0) >= MIN_SCORE]
    if not good and not drops:
        print("[presenter] nothing worth sending this run")
        return
    lines = []
    if good:
        lines.append("== NEW DEALS ==\n")
        for o in good:
            lines.append(
                f"[{o['score']}/10 - {o['verdict'].upper()}] {o['title']}\n"
                f"  {o['reason']}\n"
                f"  Red flags: {', '.join(o.get('red_flags', [])) or 'none'}\n  {o['url']}\n")
    if drops:
        lines.append("== PRICE DROPS ON WATCHED ITEMS ==\n")
        for d in drops:
            lines.append(f"{d['title']}\n  was ${d['start_price']:.2f} -> now ${d['price']:.2f} "
                         f"(-{d['drop']:.0f}%)\n  {d['url']}\n")
    body = "\n".join(lines)
    if not os.getenv("SMTP_USER"):          # email not set up yet: show on screen
        print("\n[presenter] email not set up, printing instead:\n")
        print(body)
        return
    msg = EmailMessage()
    msg["Subject"] = f"Newegg: {len(good)} new deals, {len(drops)} price drops"
    msg["From"] = os.environ["SMTP_USER"]
    msg["To"] = os.environ["MAIL_TO"]
    msg.set_content(body)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
        s.send_message(msg)
    print(f"[presenter] emailed {len(good)} deals, {len(drops)} drops")


if __name__ == "__main__":
    found = find_offers()
    new = only_new(found)
    print(f"[finder] {len(found)} matching deals, {len(new)} new")
    unscored, drops = check_watchlist(new)   # reposts of watched items first
    scored = evaluate(unscored)
    add_to_watchlist(scored)                 # then shelve the new maybes
    present(scored, drops)
