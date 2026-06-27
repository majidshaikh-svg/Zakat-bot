import os, json, logging, tempfile, base64, urllib.request, time, re, io, threading
import requests
try:
    from PIL import Image
    PIL_AVAILABLE = True
except:
    PIL_AVAILABLE = False
import anthropic
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN  = os.environ["TELEGRAM_TOKEN"]
CLAUDE_API_KEY  = os.environ["CLAUDE_API_KEY"]
ALLOWED_USER_ID = int(os.environ.get("ALLOWED_USER_ID", "0"))
SCRIPT_URL      = "https://script.google.com/macros/s/AKfycbwcUq-_msKg1pb_0VKHMZIcxoz4heumA5NuwtPyW82YaMyEN4PVX8OkgngWHR8vQaOQ/exec"
CATEGORIES      = ["Zakat", "Khair", "Asanee"]

CAT_ICON = {
    "Zakat":  "🕌",
    "Khair":  "🤲",
    "Asanee": "👨‍👩‍👧",
}

DIVIDER = "━━━━━━━━━━━━━━━━━━━━"

CONFIRM_PROMPT = (
    "\nReply:\n"
    "  ✅ YES to confirm\n"
    "  ✏️ EDIT to correct\n"
    "  ❌ NO to cancel"
)

EDIT_HELP = (
    "✏️ What to fix? Tell me e.g.:\n"
    "  - 1 is not a transaction\n"
    "  - remove 2\n"
    "  - 3 is 50000\n"
    "  - date is 23 Apr 2026\n"
    "  - 1 date is 23 Apr 2026\n"
    "  - details are Bhabhi Naseem through Rafay - covers Jan 2026\n"
    "  - 1 details are Bhabhi Naseem"
)

# Period keywords — if none found in details, warn user
PERIOD_KEYWORDS = [
    "jan","feb","mar","apr","may","jun","jul","aug","sep","oct","nov","dec",
    "ramadan","eid","monthly","quarter","annual","yearly","last month",
    "this month","2024","2025","2026","covers","period","from","to"
]

def has_period(text):
    """Check if text contains a period/coverage reference."""
    if not text:
        return False
    tl = text.lower()
    return any(kw in tl for kw in PERIOD_KEYWORDS)

def compress_image(img_bytes, max_kb=500):
    if not PIL_AVAILABLE:
        return img_bytes
    try:
        img = Image.open(io.BytesIO(img_bytes))
        max_dim = 1200
        if img.width > max_dim or img.height > max_dim:
            img.thumbnail((max_dim, max_dim), Image.LANCZOS)
        output = io.BytesIO()
        quality = 85
        while quality >= 30:
            output.seek(0)
            output.truncate()
            img.save(output, format="JPEG", quality=quality, optimize=True)
            if output.tell() <= max_kb * 1024:
                break
            quality -= 10
        return output.getvalue()
    except Exception as e:
        logger.error(f"Compress error: {e}")
        return img_bytes

def upload_to_drive(img_bytes, filename):
    try:
        img_b64 = base64.b64encode(img_bytes).decode()
        size_kb = len(img_b64) / 1024
        logger.info(f"Drive upload: {filename}, size={size_kb:.0f}KB")
        payload = {"action": "upload_image", "filename": filename, "image_b64": img_b64}
        data = json.dumps(payload).encode()
        req = urllib.request.Request(SCRIPT_URL, data=data, method="POST")
        req.add_header("Content-Type", "text/plain")
        with urllib.request.urlopen(req, timeout=60) as r:
            response_text = r.read().decode()
            result = json.loads(response_text)
        link = result.get("drive_link", "")
        return link
    except Exception as e:
        logger.error(f"Drive upload error: {type(e).__name__}: {e}")
        return ""

def rename_drive_file(drive_link, new_name):
    try:
        if not drive_link:
            return
        file_id = drive_link.split("/d/")[1].split("/")[0]
        payload = {"action": "rename_file", "file_id": file_id, "new_name": new_name}
        data = json.dumps(payload).encode()
        req = urllib.request.Request(SCRIPT_URL, data=data, method="POST")
        req.add_header("Content-Type", "text/plain")
        with urllib.request.urlopen(req, timeout=15) as r:
            pass
    except Exception as e:
        logger.error(f"Drive rename error: {e}")

def get_balances():
    rows = get_rows()
    bal = {"Zakat": 0, "Khair": 0, "Asanee": 0}
    try: bal["Khair"]  = float(str(rows[4][12]).replace(",","").replace(" ",""))
    except: pass
    try: bal["Zakat"]  = float(str(rows[4][17]).replace(",","").replace(" ",""))
    except: pass
    try: bal["Asanee"] = float(str(rows[4][22]).replace(",","").replace(" ",""))
    except: pass
    return bal

def get_rows():
    """Fetch sheet data from the Apps Script web app. Retries with backoff because
    the Charity page fires balances/transactions/insights requests to this same
    SCRIPT_URL concurrently on load, and Apps Script web apps can return an empty
    or non-JSON body under simultaneous hits — which surfaces as a confusing
    'Expecting value: line 1 column 1' JSON error with no other context."""
    url = SCRIPT_URL + "?t=" + str(int(time.time()))
    last_err = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=15) as r:
                raw = r.read().decode()
            return json.loads(raw)
        except Exception as e:
            last_err = e
            if attempt < 2:
                time.sleep(0.6 * (attempt + 1))
    raise RuntimeError(f"Sheet fetch failed after 3 attempts: {last_err}")

def append_entry(date, amount, category, details, drive_link="", raw_message="", input_type="text"):
    payload = {
        "action": "append", "date": date, "amount": amount,
        "category": category, "details": details, "drive_link": drive_link,
        "log_ref": "", "input_type": input_type, "raw_message": raw_message
    }
    data = json.dumps(payload).encode()
    req = urllib.request.Request(SCRIPT_URL, data=data, method="POST")
    req.add_header("Content-Type", "text/plain")
    with urllib.request.urlopen(req, timeout=15) as r:
        result = json.loads(r.read().decode())
    return result.get("txn_id", ""), result.get("row", 0)

def update_entry(txn_id, date=None, amount=None, category=None, details=None):
    """Edit an existing saved entry by txn_id. Requires the Apps Script web app
    to support an 'update' action — see /api/transaction/<id> PUT for details."""
    payload = {"action": "update", "txn_id": txn_id}
    if date is not None:     payload["date"] = date
    if amount is not None:   payload["amount"] = amount
    if category is not None: payload["category"] = category
    if details is not None:  payload["details"] = details
    data = json.dumps(payload).encode()
    req = urllib.request.Request(SCRIPT_URL, data=data, method="POST")
    req.add_header("Content-Type", "text/plain")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read().decode()
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"success": False, "error": "Apps Script did not return JSON — the 'update' action likely isn't implemented in Code.gs yet"}
    except Exception as e:
        return {"success": False, "error": str(e)}

client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)

# ══════════════════════════════════════════════════════
# INSIGHT CACHE — event-driven, not time-driven
# Claude only runs when data actually changes
# ══════════════════════════════════════════════════════
_insight_cache = {
    "charity":  {"insights": [], "generated": None, "row_count": 0, "error": None},
    "pulse":    {"insights": [], "generated": None, "action_count": 0},
    "cards":    {"insights": [], "generated": None, "txn_count": 0},
}
_cache_lock = threading.Lock()

def _get_sheet_row_count():
    try:
        rows = get_rows()
        return len([r for r in rows[1:] if len(r) >= 5 and str(r[0]).startswith("TXN-")])
    except:
        return 0

def _generate_charity_insights():
    """Generate charity insights and update cache. Only called on data change."""
    try:
        rows = get_rows()
        data_rows = []
        for row in rows[1:]:
            if len(row) < 5: continue
            if str(row[0]).startswith("TXN-"):
                date=str(row[1]).strip(); amount=str(row[2]).strip()
                head=str(row[3]).strip(); cat=str(row[4]).strip()
                details=str(row[5]).strip() if len(row)>5 else ""
            else:
                date=str(row[1]).strip(); amount=str(row[2]).strip()
                head=str(row[3]).strip(); cat=str(row[4]).strip()
                details=str(row[5]).strip() if len(row)>5 else ""
            if cat not in CATEGORIES: continue
            try: float(str(amount).replace(",",""))
            except: continue
            data_rows.append(f"{date} | {head} | PKR {amount} | {cat} | {details}")

        last_100 = data_rows[-100:] if len(data_rows) > 100 else data_rows
        if not last_100:
            return []

        today = time.strftime("%d-%B-%Y")
        current_month = time.strftime("%B %Y")
        prompt = f"""You are analysing Zakat and charity transaction history for Majid.
Today's date is {today}. Current month is {current_month}.

Here are the last {len(last_100)} transactions (oldest to newest):
Format: TXN_DATE | HEAD/CHANNEL | AMOUNT | CATEGORY | DETAILS
Note: TXN_DATE is when entry was recorded. DETAILS contains the actual coverage period.
{chr(10).join(last_100)}

═══ CRITICAL INSTRUCTIONS ═══

DATE INTERPRETATION:
- TXN_DATE = entry date only — do NOT use for gap analysis
- DETAILS field contains actual coverage period — always read this first
- Example: details "Bhabhi Naseem - covers Jan to Mar 2026" means paid up to Mar 2026
- Only flag missing AFTER the coverage period has expired

RECIPIENT FREQUENCY:
- WEEKLY (flag if no entry in last 10 days): Biryani Dubai
- MONTHLY (flag if no entry in last 45 days): Bhabhi Naseem, Bhabhi Madiha, Panoaqil Homes, Langar
- ANNUAL/SEASONAL (NEVER flag as missing): Ramadan giving, Eid giving, Waja Mine, Daig, any Masjid donation
- Do NOT flag any recipient not in the above lists unless clearly monthly

CURRENT DATE RULES:
- Today is {today} — do NOT flag as missing if last paid in current or previous month
- Ramadan is annual — never flag outside Ramadan season
- Eid ul Fitr and Eid ul Adha are twice yearly — never flag these
- Never say "missing since June 2026" if we are currently in June 2026

NAMING CONTEXT:
- "Mama Raja", "Ada Lala", "Bhabhi Naseem", "Bhabhi Madiha", "Maulana Jamshed" are people
- "through Rafay/Asif/Kamran" means via that person as a channel — not the recipient
- Panoaqil, Karachi, Dubai, Bahrain are LOCATIONS not recipients
- "Panoaqil Homes" is a specific charity — track separately from location "Panoaqil"
- "Biryani Dubai" is a weekly food charity in Dubai — track weekly

BALANCE CONTEXT:
- Negative Khair balance = more given than received — this is POSITIVE, never flag as problem
- Only comment on balances if truly anomalous

Analyse and generate exactly 4 insights.
Priority order: weekly missing > monthly missing > positive patterns > general observations.
Include at least 1 positive insight.

Each insight must have:
- "type": "warning" | "amber" | "positive"
- "text": concise, max 15 words, mention specific name/cause

Return ONLY a JSON array:
[
  {{"type": "warning", "text": "..."}},
  {{"type": "amber",   "text": "..."}},
  {{"type": "positive","text": "..."}},
  {{"type": "warning", "text": "..."}}
]"""

        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = response.content[0].text.strip()
        cleaned = raw.replace("```json", "").replace("```", "").strip()
        try:
            insights = json.loads(cleaned)
        except json.JSONDecodeError:
            # Claude sometimes adds stray text around the array — pull out the [...] block
            m = re.search(r"\[.*\]", cleaned, re.DOTALL)
            if not m:
                raise ValueError(f"Claude returned non-JSON: {cleaned[:150]!r}")
            insights = json.loads(m.group(0))
        row_count = _get_sheet_row_count()
        with _cache_lock:
            _insight_cache["charity"]["insights"]  = insights
            _insight_cache["charity"]["generated"] = time.strftime("%Y-%m-%d %H:%M")
            _insight_cache["charity"]["row_count"] = row_count
            _insight_cache["charity"]["error"] = None
        logger.info(f"Charity insights regenerated — {len(insights)} insights, {row_count} rows")
        return insights
    except Exception as e:
        logger.error(f"Charity insights generation error: {e}")
        with _cache_lock:
            _insight_cache["charity"]["error"] = str(e)
        return []

def fmt(n):
    return f"{int(n):,}"

def format_balances(bal):
    lines = []
    for c in CATEGORIES:
        icon = CAT_ICON.get(c, "💰")
        val = bal.get(c, 0)
        lines.append(f"  {icon} {c}:   PKR {fmt(val)}")
    return "\n".join(lines)

def format_date_display(date_str):
    if not date_str:
        return "-"
    if "T" in date_str:
        date_str = date_str.split("T")[0]
        try:
            parts = date_str.split("-")
            months = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
            d = int(parts[2])
            m = months[int(parts[1])-1]
            y = parts[0][2:]
            return f"{d:02d}-{m}-{y}"
        except:
            return date_str
    return date_str

def clean_details(details, amount, category):
    if not details:
        return details
    cleaned = details
    patterns = [
        rf"(?i)^is\s+{re.escape(category)}\s+pkr\s+[\d,]+\s*",
        rf"(?i)^is\s+[\d,]+\s*",
        rf"(?i)pkr\s+{re.escape(str(int(amount)))}\s*",
    ]
    for p in patterns:
        cleaned = re.sub(p, "", cleaned).strip()
    cleaned = re.sub(r'\s*[=\-]\s*[\d,]+\s*$', '', cleaned).strip()
    cleaned = cleaned.strip(" -=|,")
    return cleaned if cleaned else details

def format_entry_list(results, cat_filter=None):
    total = sum(e["amount"] for e in results)
    label = cat_filter or "All"
    msg = f"📋 Last {len(results)} {label} Entries\n{DIVIDER}\n"
    for i, e in enumerate(results):
        icon = CAT_ICON.get(e["category"], "💰")
        date_str = format_date_display(e["date"]) if e["date"] else "-"
        details = clean_details(e.get("details",""), e.get("amount",0), e.get("category",""))
        txn = e.get("txn_id", "")
        txn_str = f" | {txn}" if txn else ""
        msg += f"{i+1}. {icon} {e['category']} | PKR {fmt(e['amount'])} | 📅 {date_str}{txn_str}\n"
        if details:
            msg += f"   📝 {details}\n"
        msg += "\n"
    msg += f"{DIVIDER}\n💵 Total: PKR {fmt(total)}"
    if any(e.get("txn_id") for e in results):
        msg += "\n\nReply \"screenshot N\" or \"log N\" to see source"
    return msg

def format_pending(entries):
    msg = ""
    for i, e in enumerate(entries):
        icon = CAT_ICON.get(e["category"], "💰")
        date_str = format_date_display(e["date"]) if e["date"] else "-"
        details = clean_details(e.get("details",""), e.get("amount",0), e.get("category",""))
        msg += f"{i+1}. {icon} {e['category']} | PKR {fmt(e['amount'])} | 📅 {date_str}\n"
        if details:
            msg += f"   📝 {details}\n"
        msg += "\n"
    return msg

def check_duplicates(entries, rows):
    dup_found = []
    recent_rows = [r for r in rows[-20:] if len(r) >= 5]
    for row in recent_rows:
        if str(row[0]).startswith("TXN-"):
            row_date = str(row[1]).strip()
            row_amt_str = str(row[2]).strip()
            row_cat = str(row[4]).strip()
            row_det = str(row[5]).strip() if len(row) > 5 else ""
        else:
            row_date = str(row[1]).strip()
            row_amt_str = str(row[2]).strip()
            row_cat = str(row[4]).strip() if len(row) > 4 else ""
            row_det = str(row[5]).strip() if len(row) > 5 else ""
        if not row_date:
            continue
        try: row_amt = float(row_amt_str.replace(",",""))
        except: continue
        for entry in entries:
            same_amount = int(row_amt) == int(entry.get("amount", -1))
            same_category = row_cat.lower() == entry.get("category","").lower()
            det = entry.get("details","")
            similar_desc = (
                row_det and det and (
                    row_det.lower() in det.lower() or
                    det.lower() in row_det.lower()
                )
            )
            if same_amount and same_category and similar_desc:
                icon = CAT_ICON.get(row_cat, "💰")
                dup_found.append(
                    f"  {icon} {row_cat} | PKR {fmt(int(row_amt))} | 📅 {format_date_display(row_date)}\n  📝 {row_det}"
                )
    return dup_found

def parse_date_string(date_text):
    date_text = date_text.strip()
    months_map = {
        "jan":1,"feb":2,"mar":3,"apr":4,"may":5,"jun":6,
        "jul":7,"aug":8,"sep":9,"oct":10,"nov":11,"dec":12,
        "january":1,"february":2,"march":3,"april":4,"june":6,
        "july":7,"august":8,"september":9,"october":10,"november":11,"december":12,
    }
    month_names = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
    if re.match(r'\d{1,2}-[A-Za-z]{3}-\d{2}$', date_text):
        return date_text
    m = re.search(r'(\d{1,2})?\s*([A-Za-z]+)\s*(\d{2,4})', date_text, re.I)
    if m:
        day  = int(m.group(1)) if m.group(1) else 1
        mon  = m.group(2).lower()
        year = m.group(3)
        if len(year) == 4: year = year[2:]
        if mon in months_map:
            return f"{day:02d}-{month_names[months_map[mon]-1]}-{year}"
    return None

def apply_corrections(entries, text):
    corrections = []
    to_remove = set()
    tl = text.lower().strip()
    entries = [dict(e) for e in entries]
    for m in re.finditer(r'(?:remove|delete)\s+(\d+)', tl):
        idx = int(m.group(1)) - 1
        if 0 <= idx < len(entries):
            to_remove.add(idx)
            corrections.append(f"Removed entry {idx+1}")
    for m in re.finditer(r'(\d+)\s+is\s+not\s+a\s+transaction', tl):
        idx = int(m.group(1)) - 1
        if 0 <= idx < len(entries):
            to_remove.add(idx)
            corrections.append(f"Removed entry {idx+1}")
    for m in re.finditer(r'(\d+)\s+is\s+([\d,]+)', text):
        idx = int(m.group(1)) - 1
        amt_str = m.group(2).replace(",","")
        if amt_str.isdigit() and 0 <= idx < len(entries) and idx not in to_remove:
            old = entries[idx]["amount"]
            entries[idx]["amount"] = int(amt_str)
            corrections.append(f"Entry {idx+1} amount: PKR {fmt(old)} → PKR {fmt(int(amt_str))}")
    m = re.search(r'(\d+)\s+date\s+is\s+(.+)', tl)
    if m:
        idx = int(m.group(1)) - 1
        parsed = parse_date_string(m.group(2))
        if parsed and 0 <= idx < len(entries) and idx not in to_remove:
            entries[idx]["date"] = parsed
            corrections.append(f"Entry {idx+1} date → {parsed}")
    else:
        m = re.search(r'date\s+is\s+(.+)', tl)
        if m:
            parsed = parse_date_string(m.group(1))
            if parsed:
                for i in range(len(entries)):
                    if i not in to_remove:
                        entries[i]["date"] = parsed
                corrections.append(f"Date set to {parsed} for all entries")
    m = re.search(r'(\d+)\s+details?\s+(?:are|is)\s+(.+)', text, re.I)
    if m:
        idx = int(m.group(1)) - 1
        new_det = m.group(2).strip()
        if 0 <= idx < len(entries) and idx not in to_remove:
            entries[idx]["details"] = new_det
            corrections.append(f"Entry {idx+1} details → {new_det}")
    else:
        m = re.search(r'details?\s+(?:are|is)\s+(.+)', text, re.I)
        if m:
            new_det = m.group(1).strip()
            for i in range(len(entries)):
                if i not in to_remove:
                    entries[i]["details"] = new_det
            corrections.append(f"Details set to: {new_det}")
    updated = [e for i, e in enumerate(entries) if i not in to_remove]
    return updated, corrections

def detect_image_media_type(img_bytes):
    """Sniff actual image format from magic bytes — never trust a caller-supplied label.
    Browsers/iOS send screenshots as PNG; Telegram always sends JPEG. Anthropic's
    vision API requires the declared media_type to match the real bytes, so a
    mismatch (e.g. PNG data labeled image/jpeg) causes the API call to error out."""
    if img_bytes[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if img_bytes[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if img_bytes[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if img_bytes[:4] == b"RIFF" and img_bytes[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"  # fallback for anything unrecognized

def extract(text, img_b64=None, recent=""):
    content = []
    if img_b64:
        try:
            media_type = detect_image_media_type(base64.b64decode(img_b64))
        except Exception:
            media_type = "image/jpeg"
        content.append({"type":"image","source":{"type":"base64","media_type":media_type,"data":img_b64}})
    content.append({"type":"text","text": text or "See attached."})
    today = time.strftime("%d-%b-%y")
    system = f"""Extract ALL charity payment entries. Categories: Zakat, Khair, Asanee.
Return ONLY a JSON array:
[{{"date":"19-Apr-26","amount":50000,"category":"Zakat","details":"Mama Raja - covers Jan 2026"}}]
If nothing found: [{{"error":"reason"}}]
Rules:
- Amount in PKR. 1m=1000000, 1 lakh=100000, 1k=1000. 'rs', 'Rs', 'RS', 'rupees', 'rupee' all mean PKR — extract the number before them as the amount
- Date column = today ({today}) — the date this entry is being recorded
- Details MUST include the coverage period if mentioned (e.g. "Dr Malla zakat - covers Dec 2025 to Feb 2026")
- If user mentions a period/month/year in their message, always include it in details
- Fix spelling mistakes in category names
- Domestic staff payments (maid, cook, driver, cleaner, guard, helper, servant) = Khair
- Home maintenance, utility bills, household expenses = Khair
- Details should be clean human-readable description. Do NOT repeat the amount or category.
Recent entries:
{recent}"""
    r = client.messages.create(model="claude-sonnet-4-6", max_tokens=1000, system=system, messages=[{"role":"user","content":content}])
    raw = r.content[0].text.strip().replace("```json","").replace("```","").strip()
    result = json.loads(raw)
    if isinstance(result, dict): result = [result]
    for e in result:
        if not e.get("date") or e.get("date") in ["unknown", ""]:
            e["date"] = today
    return result

# Pakistan-based recipients that need a channel mentioned
PAKISTAN_RECIPIENTS = [
    "bhabhi naseem", "bhabhi madiha", "maulana jamshed",
    "ada lala", "mama raja", "panoaqil homes",
    "dr malla", "mufti najeeb", "homes"
]

def check_period_warning(entries, raw_text=""):
    """Check if any entry is missing a coverage period in details."""
    warnings = []
    for i, e in enumerate(entries):
        details = e.get("details", "")
        combined = (details + " " + raw_text).lower()
        if not has_period(combined):
            icon = CAT_ICON.get(e.get("category",""), "💰")
            warnings.append(
                f"  {icon} Entry {i+1}: {e.get('details','—')} | PKR {fmt(e.get('amount',0))}"
            )
    return warnings

def check_channel_warning(entries, raw_text=""):
    """Check if Pakistan-based recipients are missing a channel/through mention."""
    warnings = []
    channel_keywords = ["through", "via", "rafay", "asif", "kamran", "direct", "bank", "transfer", "easypaisa", "jazzcash"]
    for i, e in enumerate(entries):
        details = e.get("details", "")
        combined = (details + " " + raw_text).lower()
        # Check if recipient is Pakistan-based
        is_pak_recipient = any(r in combined for r in PAKISTAN_RECIPIENTS)
        if is_pak_recipient:
            has_channel = any(kw in combined for kw in channel_keywords)
            if not has_channel:
                icon = CAT_ICON.get(e.get("category",""), "💰")
                warnings.append(
                    f"  {icon} Entry {i+1}: {e.get('details','—')} | PKR {fmt(e.get('amount',0))}"
                )
    return warnings

def build_confirmation_msg(entries, bal, dup_found, raw_text=""):
    msg = (
        f"✅ {len(entries)} entr{'y' if len(entries)==1 else 'ies'} found:\n\n"
        f"{format_pending(entries)}"
        f"{DIVIDER}\n"
        f"💳 Current Balances:\n"
        f"{format_balances(bal)}\n"
        f"{DIVIDER}\n"
    )
    if dup_found:
        msg += f"⚠️ Possible duplicate found:\n\n" + "\n\n".join(dup_found[:3]) + f"\n\n{DIVIDER}\n"

    # Collect all warnings
    period_warnings  = check_period_warning(entries, raw_text)
    channel_warnings = check_channel_warning(entries, raw_text)

    if period_warnings or channel_warnings:
        msg += f"📋 A few things to check:\n\n"
        if period_warnings:
            msg += (
                f"📅 No coverage period mentioned:\n"
                + "\n".join(period_warnings) + "\n"
                f"   e.g. \"covers Jan to Mar 2026\"\n\n"
            )
        if channel_warnings:
            msg += (
                f"👤 No channel mentioned (Pakistan recipient):\n"
                + "\n".join(channel_warnings) + "\n"
                f"   e.g. \"through Rafay\" or \"direct bank transfer\"\n\n"
            )
        msg += f"{DIVIDER}\n"

    msg += CONFIRM_PROMPT
    return msg

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        bal = get_balances()
        msg = (
            f"🌙 Majid Charity Tracker\n"
            f"{DIVIDER}\n"
            f"💳 Balances:\n"
            f"{format_balances(bal)}\n"
            f"{DIVIDER}\n"
            f"📩 Send text, voice or screenshot!\n"
            f"💡 Tip: Always mention the period covered\n"
            f"   e.g. Dr Malla zakat - covers Jan to Mar 2026"
        )
        await update.message.reply_text(msg)
    except Exception as e:
        await update.message.reply_text(f"Bot running! Sheet error: {e}")

async def balances_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USER_ID and update.effective_user.id != ALLOWED_USER_ID: return
    try:
        bal = get_balances()
        msg = (f"💳 Balances\n{DIVIDER}\n{format_balances(bal)}")
        await update.message.reply_text(msg)
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")

async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USER_ID and update.effective_user.id != ALLOWED_USER_ID: return
    text = update.message.text.strip()
    tl = text.lower()
    pending = ctx.user_data.get("pending", [])
    waiting_edit = ctx.user_data.get("waiting_edit", False)

    if tl in ["yes","y","confirm","ok"]:
        if pending:
            try:
                saved_txns = []
                raw_message = ctx.user_data.get("raw_message", "")
                input_type = ctx.user_data.get("input_type", "text")
                img_bytes = ctx.user_data.get("img_bytes", None)
                drive_link = ""
                if img_bytes:
                    tmp_name = f"TXN-tmp-{int(time.time())}.jpg"
                    compressed = compress_image(img_bytes)
                    drive_link = upload_to_drive(compressed, tmp_name)
                for entry in pending:
                    details = clean_details(entry.get("details",""), entry.get("amount",0), entry.get("category",""))
                    txn_id, row = append_entry(
                        entry["date"], entry["amount"], entry["category"], details,
                        drive_link=drive_link, raw_message=raw_message, input_type=input_type
                    )
                    saved_txns.append(txn_id)
                if drive_link and saved_txns and saved_txns[0]:
                    rename_drive_file(drive_link, f"{saved_txns[0]}.jpg")
                new = get_balances()
                ctx.user_data["pending"] = []
                ctx.user_data["waiting_edit"] = False
                ctx.user_data["img_bytes"] = None
                ctx.user_data["raw_message"] = ""
                ctx.user_data["input_type"] = "text"
                txn_str = " | ".join(t for t in saved_txns if t)
                msg = (f"✅ Saved! {txn_str}\n{DIVIDER}\n💳 New Balances:\n{format_balances(new)}")
                await update.message.reply_text(msg)
            except Exception as e:
                await update.message.reply_text(f"Error saving: {e}")
        else:
            await update.message.reply_text("No pending entry.")
        return

    if tl in ["no","cancel"]:
        ctx.user_data["pending"] = []
        ctx.user_data["waiting_edit"] = False
        ctx.user_data["img_bytes"] = None
        ctx.user_data["raw_message"] = ""
        await update.message.reply_text("❌ Cancelled.")
        return

    if tl in ["edit","e"] and pending:
        ctx.user_data["waiting_edit"] = True
        await update.message.reply_text(EDIT_HELP)
        return

    if waiting_edit and pending:
        updated, corrections = apply_corrections(list(pending), text)
        ctx.user_data["waiting_edit"] = False
        if corrections:
            ctx.user_data["pending"] = updated
            if not updated:
                ctx.user_data["pending"] = []
                await update.message.reply_text("✅ All entries removed. Nothing to save.")
                return
            try: bal = get_balances()
            except: bal = {}
            summary = "\n".join(f"  - {c}" for c in corrections)
            raw_text = ctx.user_data.get("raw_message", "")
            period_warnings  = check_period_warning(updated, raw_text)
            channel_warnings = check_channel_warning(updated, raw_text)
            period_msg = ""
            if period_warnings or channel_warnings:
                period_msg = f"\n📋 Still missing:\n"
                if period_warnings:
                    period_msg += (
                        f"📅 No coverage period:\n"
                        + "\n".join(period_warnings) + "\n"
                        f"   e.g. \"covers Jan to Mar 2026\"\n"
                    )
                if channel_warnings:
                    period_msg += (
                        f"👤 No channel (Pakistan recipient):\n"
                        + "\n".join(channel_warnings) + "\n"
                        f"   e.g. \"through Rafay\"\n"
                    )
                period_msg += "\n"
            msg = (
                f"✏️ Updated:\n{summary}\n\n"
                f"{format_pending(updated)}"
                f"{DIVIDER}\n"
                f"💳 Current Balances:\n"
                f"{format_balances(bal)}\n"
                f"{DIVIDER}\n"
                f"{period_msg}"
                f"{CONFIRM_PROMPT}"
            )
            await update.message.reply_text(msg)
        else:
            ctx.user_data["waiting_edit"] = True
            await update.message.reply_text(f"Couldn't understand that correction.\n\n{EDIT_HELP}")
        return

    m = re.match(r'(screenshot|photo|image|log|message)\s+(\d+)', tl)
    if m:
        ref_type = m.group(1)
        ref_idx = int(m.group(2)) - 1
        last_results = ctx.user_data.get("last_results", [])
        if 0 <= ref_idx < len(last_results):
            entry = last_results[ref_idx]
            txn_id = entry.get("txn_id", "")
            if ref_type in ["screenshot","photo","image"]:
                drive_link = entry.get("drive_link", "")
                if drive_link:
                    await update.message.reply_text(f"📸 {txn_id} screenshot:\n{drive_link}")
                else:
                    await update.message.reply_text(f"No screenshot found for {txn_id}")
            else:
                raw = entry.get("raw_message", "")
                input_t = entry.get("input_type", "")
                if raw:
                    await update.message.reply_text(f"📋 {txn_id} original message:\nType: {input_t}\n\n{raw}")
                else:
                    await update.message.reply_text(f"No log found for {txn_id}")
        else:
            await update.message.reply_text("Entry not found. Try searching first.")
        return

    if any(w in tl for w in ["last","show","share","find","search","entries","list"]):
        try:
            rows = get_rows()
        except Exception as e:
            await update.message.reply_text(f"Could not load sheet: {e}")
            return
        n = 10
        for word in tl.split():
            if word.isdigit(): n = int(word)
        cat_filter = None
        for cat in CATEGORIES:
            if cat.lower() in tl:
                cat_filter = cat
                break
        keyword = None
        m = re.search(r'mention(?:ing)?\s+(\w+)', tl)
        if m: keyword = m.group(1)
        else:
            for trigger in ["with","about","for"]:
                if trigger in tl.split():
                    parts = tl.split(trigger)
                    if len(parts) > 1 and parts[-1].strip():
                        keyword = parts[-1].strip().split()[0]
                    break
        results = []
        for row in rows[1:]:
            if len(row) < 4: continue
            if str(row[0]).startswith("TXN-"):
                txn_id=str(row[0]).strip(); date=str(row[1]).strip(); amount=str(row[2]).strip()
                cat=str(row[4]).strip() if len(row)>4 else ""; details=str(row[5]).strip() if len(row)>5 else ""
                drive_link=str(row[6]).strip() if len(row)>6 else ""; raw_msg=str(row[7]).strip() if len(row)>7 else ""
            else:
                txn_id=""; date=str(row[1]).strip(); amount=str(row[2]).strip()
                cat=str(row[4]).strip() if len(row)>4 else ""; details=str(row[5]).strip() if len(row)>5 else ""
                drive_link=""; raw_msg=""
            if cat not in CATEGORIES: continue
            if cat_filter and cat.lower() != cat_filter.lower(): continue
            if keyword and keyword.lower() not in details.lower() and keyword.lower() not in date.lower(): continue
            try: amt = float(str(amount).replace(",",""))
            except: amt = 0
            results.append({"txn_id":txn_id,"date":date,"amount":amt,"category":cat,"details":details,"drive_link":drive_link,"raw_message":raw_msg})
        results = results[-n:]
        results.reverse()
        if not results:
            await update.message.reply_text("No entries found.")
            return
        ctx.user_data["last_results"] = results
        await update.message.reply_text(format_entry_list(results, cat_filter))
        return

    ctx.user_data["raw_message"] = text
    ctx.user_data["input_type"] = "text"
    await update.message.reply_text("🔍 Analyzing...")
    try:
        rows = get_rows()
        recent_rows = rows[-10:]
        recent_parts = []
        for r in recent_rows:
            if len(r) < 4: continue
            if str(r[0]).startswith("TXN-"): recent_parts.append(f"{r[1]}|{r[2]}|{r[4]}|{r[5] if len(r)>5 else ''}")
            else: recent_parts.append(f"{r[0]}|{r[1]}|{r[3]}|{r[4] if len(r)>4 else ''}")
        recent = "\n".join(recent_parts)
    except: recent = ""; rows = []
    try:
        entries = extract(text, recent=recent)
        if not entries or "error" in entries[0]:
            err = entries[0].get("error","unknown") if entries else "unknown"
            await update.message.reply_text(f"Could not extract: {err}\n\nTry again.")
            return
        dup_found = check_duplicates(entries, rows)
        ctx.user_data["pending"] = entries
        ctx.user_data["waiting_edit"] = False
        try: bal = get_balances()
        except: bal = {}
        await update.message.reply_text(build_confirmation_msg(entries, bal, dup_found, raw_text=text))
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")

async def handle_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USER_ID and update.effective_user.id != ALLOWED_USER_ID: return
    await update.message.reply_text("🎙 Transcribing...")
    try:
        file = await ctx.bot.get_file(update.message.voice.file_id)
        with tempfile.NamedTemporaryFile(suffix=".ogg") as tmp:
            await file.download_to_drive(tmp.name)
            with open(tmp.name,"rb") as f: audio_b64 = base64.b64encode(f.read()).decode()
        r = client.messages.create(model="claude-sonnet-4-6", max_tokens=300,
            system="Transcribe exactly. Return only transcription.",
            messages=[{"role":"user","content":[{"type":"document","source":{"type":"base64","media_type":"audio/ogg","data":audio_b64}},{"type":"text","text":"Transcribe."}]}])
        transcript = r.content[0].text.strip()
        await update.message.reply_text(f"🎙 Heard: {transcript}")
        ctx.user_data["raw_message"] = transcript
        ctx.user_data["input_type"] = "voice"
        update.message.text = transcript
        await handle_text(update, ctx)
    except Exception as e:
        await update.message.reply_text(f"Voice error: {e}")

async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USER_ID and update.effective_user.id != ALLOWED_USER_ID: return
    await update.message.reply_text("📸 Reading screenshot...")
    try:
        file = await ctx.bot.get_file(update.message.photo[-1].file_id)
        with tempfile.NamedTemporaryFile(suffix=".jpg") as tmp:
            await file.download_to_drive(tmp.name)
            with open(tmp.name,"rb") as f:
                img_bytes = f.read()
                img_b64 = base64.b64encode(img_bytes).decode()
        caption = update.message.caption or ""
        ctx.user_data["raw_message"] = caption or "[screenshot]"
        ctx.user_data["input_type"] = "screenshot"
        ctx.user_data["img_bytes"] = img_bytes
        try:
            rows = get_rows()
            recent_rows = rows[-10:]
            recent_parts = []
            for r in recent_rows:
                if len(r) < 4: continue
                if str(r[0]).startswith("TXN-"): recent_parts.append(f"{r[1]}|{r[2]}|{r[4]}|{r[5] if len(r)>5 else ''}")
                else: recent_parts.append(f"{r[0]}|{r[1]}|{r[3]}|{r[4] if len(r)>4 else ''}")
            recent = "\n".join(recent_parts)
        except: recent = ""; rows = []
        entries = extract(caption, img_b64=img_b64, recent=recent)
        if not entries or "error" in entries[0]:
            await update.message.reply_text("Could not extract. Add a caption.")
            return
        dup_found = check_duplicates(entries, rows)
        ctx.user_data["pending"] = entries
        ctx.user_data["waiting_edit"] = False
        try: bal = get_balances()
        except: bal = {}
        await update.message.reply_text(build_confirmation_msg(entries, bal, dup_found, raw_text=caption))
    except Exception as e:
        await update.message.reply_text(f"Photo error: {e}")


# ══════════════════════════════════════════════════════
# FLASK API
# ══════════════════════════════════════════════════════
from flask import Flask, request, jsonify
from flask_cors import CORS

flask_app = Flask(__name__)
CORS(flask_app)

def row_to_entry(row):
    if str(row[0]).startswith("TXN-"):
        txn_id=str(row[0]).strip(); date=str(row[1]).strip(); amount=str(row[2]).strip()
        cat=str(row[4]).strip() if len(row)>4 else ""; details=str(row[5]).strip() if len(row)>5 else ""
        drive_link=str(row[6]).strip() if len(row)>6 else ""; raw_msg=str(row[7]).strip() if len(row)>7 else ""
    else:
        txn_id=""; date=str(row[1]).strip(); amount=str(row[2]).strip()
        cat=str(row[4]).strip() if len(row)>4 else ""; details=str(row[5]).strip() if len(row)>5 else ""
        drive_link=""; raw_msg=""
    if cat not in CATEGORIES: return None
    try: amt=float(str(amount).replace(",",""))
    except: amt=0
    return {"txn_id":txn_id,"date":date,"amount":amt,"category":cat,"details":details,"drive_link":drive_link,"raw_message":raw_msg,"input_type":""}

@flask_app.route("/api/health")
def api_health():
    return jsonify({"status":"ok","service":"Finance Hub - Zakat API"})

@flask_app.route("/api/balances")
def api_balances():
    try: return jsonify(get_balances())
    except Exception as e: return jsonify({"error":str(e)}),500

@flask_app.route("/api/transactions")
def api_transactions():
    try:
        limit=int(request.args.get("limit",20))
        cat_filter=request.args.get("category",None)
        rows=get_rows(); results=[]
        for row in rows[1:]:
            if len(row)<5: continue
            e=row_to_entry(row)
            if not e: continue
            if cat_filter and e["category"].lower()!=cat_filter.lower(): continue
            results.append(e)
        return jsonify({"transactions":list(reversed(results[-limit:]))})
    except Exception as e: return jsonify({"error":str(e)}),500

@flask_app.route("/api/transaction/<txn_id>", methods=["GET","PUT"])
def api_transaction_detail(txn_id):
    try:
        if request.method == "GET":
            rows = get_rows()
            for row in rows[1:]:
                e = row_to_entry(row)
                if e and e["txn_id"] == txn_id:
                    return jsonify(e)
            return jsonify({"error": "Transaction not found"}), 404
        # PUT — edit an existing entry
        data = request.get_json() or {}
        result = update_entry(
            txn_id,
            date=data.get("date"),
            amount=data.get("amount"),
            category=data.get("category"),
            details=data.get("details"),
        )
        if not result.get("success"):
            return jsonify({"error": result.get("error", "Update failed")}), 500
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@flask_app.route("/api/analyze", methods=["POST"])
def api_analyze():
    try:
        data=request.get_json()
        text=data.get("text","")
        img_b64=data.get("image_b64",None)
        rows=get_rows()
        recent_parts=[]
        for r in rows[-10:]:
            if len(r)<4: continue
            if str(r[0]).startswith("TXN-"): recent_parts.append(f"{r[1]}|{r[2]}|{r[4]}|{r[5] if len(r)>5 else ''}")
            else: recent_parts.append(f"{r[0]}|{r[1]}|{r[3]}|{r[4] if len(r)>4 else ''}")
        entries=extract(text,img_b64=img_b64,recent="\n".join(recent_parts))
        if not entries or "error" in entries[0]:
            return jsonify({"error":entries[0].get("error","unknown") if entries else "unknown"}),400
        e=entries[0]
        dup=check_duplicates(entries,rows)
        e["confidence"]=78 if dup else 92
        e["dup_warning"]=dup[0] if dup else None
        # Period and channel warnings for app
        period_warnings  = check_period_warning(entries, text)
        channel_warnings = check_channel_warning(entries, text)
        e["period_warning"]  = len(period_warnings) > 0
        e["channel_warning"] = len(channel_warnings) > 0
        return jsonify(e)
    except Exception as ex:
        return jsonify({"error":str(ex)}),500

@flask_app.route("/api/save", methods=["POST"])
def api_save():
    try:
        data=request.get_json()
        date=data.get("date",time.strftime("%d-%b-%y")); amount=data.get("amount",0)
        category=data.get("category","Zakat"); details_raw=data.get("details","")
        drive_link=data.get("drive_link",""); input_type=data.get("input_type","app")
        img_b64=data.get("image_b64",None)
        details=clean_details(details_raw,amount,category)
        if img_b64 and not drive_link:
            img_bytes=base64.b64decode(img_b64); compressed=compress_image(img_bytes)
            drive_link=upload_to_drive(compressed,f"TXN-tmp-{int(time.time())}.jpg")
        txn_id,row=append_entry(date,amount,category,details,drive_link=drive_link,raw_message=details_raw,input_type=input_type)
        if drive_link and txn_id: rename_drive_file(drive_link,f"{txn_id}.jpg")
        return jsonify({"success":True,"txn_id":txn_id,"row":row,"balances":get_balances()})
    except Exception as e: return jsonify({"error":str(e)}),500

@flask_app.route("/api/charity/insights", methods=["GET","OPTIONS"])
def api_charity_insights():
    if request.method == "OPTIONS":
        r = jsonify({})
        r.headers["Access-Control-Allow-Origin"] = "*"
        r.headers["Access-Control-Allow-Headers"] = "*"
        return r, 200
    try:
        force = request.args.get("force", "").lower() in ("1", "true", "yes")
        if not force:
            # Return cache if available
            with _cache_lock:
                cached = _insight_cache["charity"]
                if cached["insights"] and cached["generated"]:
                    r = jsonify({
                        "insights":  cached["insights"],
                        "generated": cached["generated"],
                        "cached":    True
                    })
                    r.headers["Access-Control-Allow-Origin"] = "*"
                    return r, 200
        # Cache empty, or force-refresh requested — generate now
        insights = _generate_charity_insights()
        with _cache_lock:
            err = _insight_cache["charity"].get("error")
        r = jsonify({
            "insights": insights,
            "generated": time.strftime("%Y-%m-%d %H:%M"),
            "cached": False,
            "error": err if not insights else None,
        })
        r.headers["Access-Control-Allow-Origin"] = "*"
        return r, 200
    except Exception as e:
        r = jsonify({"error": str(e), "insights": []})
        r.headers["Access-Control-Allow-Origin"] = "*"
        return r, 500

@flask_app.route("/api/search", methods=["POST","OPTIONS"])
def api_search():
    if request.method == "OPTIONS":
        r = jsonify({})
        r.headers["Access-Control-Allow-Origin"] = "*"
        r.headers["Access-Control-Allow-Headers"] = "*"
        return r, 200
    try:
        data = request.get_json()
        query = data.get("query", "").strip()
        if not query:
            return jsonify({"error": "No query provided"}), 400

        rows = get_rows()
        all_txns = []
        for row in rows[1:]:
            e = row_to_entry(row)
            if e:
                all_txns.append(e)

        if not all_txns:
            r = jsonify({"answer": "No transactions found.", "transactions": []})
            r.headers["Access-Control-Allow-Origin"] = "*"
            return r, 200

        # Format with index prefix for reliable matching
        txn_lines = []
        for i, t in enumerate(all_txns):
            txn_lines.append(
                f"[{i}] {t.get('txn_id','')} | {t.get('date','')} | PKR {t.get('amount',0)} | "
                f"{t.get('category','')} | {t.get('details','')}"
            )

        today_str = time.strftime("%d-%B-%Y")

        prompt = f"""You are a charity transaction search assistant for Majid.
Today is {today_str}.

Transactions (oldest to newest), each prefixed with [index]:
{chr(10).join(txn_lines)}

User query: "{query}"

Instructions:
- Answer the query accurately
- For "last N X entries" — return most recent N matching X category or name
- For name queries — search DETAILS field
- For amount queries — calculate and show totals
- Format all dates as "15 May 2026" style
- Keep answer to 1-2 sentences

Return ONLY this JSON:
{{
  "answer": "concise answer here",
  "txn_indices": [5, 3, 1]
}}

txn_indices: [index] numbers of matching rows, most recent first, max 15.
Return ONLY the JSON."""

        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}]
        )

        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"): raw = raw[4:]
        raw = raw.strip()

        result = json.loads(raw)
        answer = result.get("answer", "")
        txn_indices = result.get("txn_indices", [])

        matched_txns = [all_txns[i] for i in txn_indices if 0 <= i < len(all_txns)]

        # Clean dates
        import datetime
        for t in matched_txns:
            raw_date = t.get("date", "")
            if "T" in raw_date:
                try:
                    d = datetime.datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
                    t["date"] = d.strftime("%d %b %Y")
                except: pass

        r = jsonify({"answer": answer, "transactions": matched_txns[:15], "total": len(matched_txns)})
        r.headers["Access-Control-Allow-Origin"] = "*"
        return r, 200

    except Exception as e:
        r = jsonify({"error": str(e), "answer": "Search failed — try again.", "transactions": []})
        r.headers["Access-Control-Allow-Origin"] = "*"
        return r, 500


@flask_app.route("/api/pulse/extract", methods=["POST","OPTIONS"])
def api_pulse_extract():
    if request.method == "OPTIONS":
        r = jsonify({})
        r.headers["Access-Control-Allow-Origin"] = "*"
        r.headers["Access-Control-Allow-Headers"] = "*"
        return r, 200
    try:
        data = request.get_json()
        text = data.get("text", "").strip()
        people_raw = data.get("people", [])  # list of {id, name, short_name}

        if not text:
            return jsonify({"error": "No text provided"}), 400

        # Build people reference string with both full and short names
        if people_raw and isinstance(people_raw[0], dict):
            people_lines = [f"  - {p.get('short_name','')} (full name: {p.get('name','')})" for p in people_raw]
            people_str = chr(10).join(people_lines)
        else:
            people_str = ', '.join(str(p) for p in people_raw)

        today = time.strftime("%d %B %Y")

        prompt = f"""Extract action details from this free text entry. Today is {today}.

Text: "{text}"

Known people in the system (short name → full name):
{people_str}

Extract:
1. title: Main action/task (concise, max 8 words)
2. people: List of short names of people mentioned — use the EXACT short_name from the known list above.
   MATCHING RULES:
   - Match by short name (exact) OR full name (exact) OR first name
   - Conservative typo correction ONLY for very close matches (1-2 chars different): "Nilesh" for "Nikesh"
   - NEVER guess a completely different name — if unsure, use the name exactly as written
   - The app will flag unrecognised names for the user to handle
3. priority: "urgent" | "high" | "normal" (infer from words like urgent/asap/critical/important)
4. due_date: ISO date YYYY-MM-DD (parse natural dates like "10th June", "next week", "tomorrow")
5. subject: Main topic/project (e.g. "Autonomous ERP", "XRG", "Hiring", "Karachi Property")
6. category: "adnoc" | "eyai" | "personal"
   - adnoc: if mentions ADNOC people (Mobin, Fahad, Omar, Mehtab) or ADNOC projects
   - eyai: if mentions EY AI, AI Hub, EY projects
   - personal: family, property, zakat, personal matters
7. detail: Any additional context from the text

Return ONLY JSON:
{{
  "title": "...",
  "people": ["Mobin", "Mehtab"],
  "priority": "urgent",
  "priority_explicit": true,
  "due_date": "2026-06-10",
  "subject": "Autonomous ERP",
  "category": "adnoc",
  "detail": "..."
}}

priority_explicit: true only if user clearly stated urgent/high/medium/normal/critical/asap. false if you inferred it."""

        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}]
        )

        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"): raw = raw[4:]
        raw = raw.strip()

        result = json.loads(raw)
        r = jsonify(result)
        r.headers["Access-Control-Allow-Origin"] = "*"
        return r, 200

    except Exception as e:
        r = jsonify({"error": str(e)})
        r.headers["Access-Control-Allow-Origin"] = "*"
        return r, 500



@flask_app.route("/api/pulse/search", methods=["POST","OPTIONS"])
def api_pulse_search():
    if request.method == "OPTIONS":
        r = jsonify({})
        r.headers["Access-Control-Allow-Origin"] = "*"
        r.headers["Access-Control-Allow-Headers"] = "*"
        return r, 200
    try:
        data = request.get_json(force=True)
        query = data.get("query","").strip()
        actions = data.get("actions", [])

        if not query or not actions:
            r = jsonify({"matched_ids": [], "explanation": "No query or actions provided"})
            r.headers["Access-Control-Allow-Origin"] = "*"
            return r, 200

        actions_text = ""
        for a in actions:
            people = ", ".join([p.get("short_name") or p.get("name","") for p in (a.get("people") or [])])
            actions_text += f"ID:{a['id']} | {a.get('title','')} | people:{people} | priority:{a.get('priority','')} | subject:{a.get('subject','')} | category:{a.get('category','')} | due:{a.get('due_date','')} | status:{a.get('status','')}\n"

        prompt = f"""You are an intelligent action search engine. The user is searching their personal action/task list.

User query: "{query}"

Actions list:
{actions_text}

Return ONLY a JSON object with:
1. "matched_ids": array of numeric IDs that match the user's query intent
2. "explanation": one short sentence explaining what you found

Match by intent, not just keywords. Examples:
- "strategy" matches actions about strategy meetings, growth, planning
- "Fahad urgent" matches urgent actions involving Fahad
- "this week" matches actions due within 7 days
- "follow up" matches actions that seem like follow-ups
- "ADNOC" matches actions in the adnoc category or mentioning ADNOC

Return ONLY valid JSON, no markdown, no explanation outside the JSON."""

        # Use the global client already initialised with CLAUDE_API_KEY
        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            messages=[{"role":"user","content":prompt}]
        )
        raw = message.content[0].text.strip()
        # Strip markdown fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        import json as _json
        result = _json.loads(raw.strip())
        r = jsonify(result)
        r.headers["Access-Control-Allow-Origin"] = "*"
        return r, 200
    except Exception as e:
        logger.error(f"Pulse search error: {e}")
        r = jsonify({"matched_ids": [], "explanation": str(e)})
        r.headers["Access-Control-Allow-Origin"] = "*"
        return r, 500

@flask_app.route("/api/transcribe", methods=["POST","OPTIONS"])
def api_transcribe():
    if request.method == "OPTIONS":
        r = jsonify({})
        r.headers["Access-Control-Allow-Origin"] = "*"
        r.headers["Access-Control-Allow-Headers"] = "*"
        return r, 200
    try:
        if 'audio' not in request.files:
            return jsonify({"error": "No audio file"}), 400

        audio_file = request.files['audio']
        audio_data = audio_file.read()

        # Use Claude to transcribe via base64
        import base64 as b64
        audio_b64 = b64.b64encode(audio_data).decode()

        # Use Anthropic API with audio input
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=500,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "Transcribe this audio recording exactly. Return only the transcribed text, nothing else."
                    },
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": audio_file.content_type or "audio/webm",
                            "data": audio_b64
                        }
                    }
                ]
            }]
        )

        transcript = response.content[0].text.strip()
        r = jsonify({"transcript": transcript})
        r.headers["Access-Control-Allow-Origin"] = "*"
        return r, 200

    except Exception as e:
        # Fallback: return empty transcript
        r = jsonify({"transcript": "", "error": str(e)})
        r.headers["Access-Control-Allow-Origin"] = "*"
        return r, 200

# ══════════════════════════════════════════════════════
# EVENT-DRIVEN REFRESH ENDPOINTS
# Called by: Google Apps Script (Sheets) + Supabase webhook
# ══════════════════════════════════════════════════════

@flask_app.route("/api/insights/refresh/charity", methods=["POST", "GET"])
def refresh_charity_insights():
    """
    Called by Google Apps Script onEdit trigger when a new row is added to the Sheet.
    Regenerates charity insights and updates cache.
    """
    try:
        current_count = _get_sheet_row_count()
        with _cache_lock:
            cached_count = _insight_cache["charity"]["row_count"]

        # Only regenerate if row count changed
        if current_count <= cached_count and _insight_cache["charity"]["insights"]:
            return jsonify({
                "status": "skipped",
                "reason": "no new data",
                "row_count": current_count
            }), 200

        # New data — regenerate
        insights = _generate_charity_insights()
        return jsonify({
            "status": "refreshed",
            "insights_count": len(insights),
            "row_count": current_count,
            "generated": time.strftime("%Y-%m-%d %H:%M")
        }), 200

    except Exception as e:
        logger.error(f"Charity refresh error: {e}")
        return jsonify({"status": "error", "error": str(e)}), 500


@flask_app.route("/api/insights/refresh/pulse", methods=["POST"])
def refresh_pulse_insights():
    """
    Called by Supabase webhook when actions table changes.
    Payload: Supabase sends {type: 'INSERT'|'UPDATE'|'DELETE', record: {...}}
    """
    try:
        payload = request.get_json(silent=True) or {}
        event_type = payload.get("type", "unknown")
        logger.info(f"Pulse refresh triggered — event: {event_type}")

        # Update action count in cache
        with _cache_lock:
            prev_count = _insight_cache["pulse"]["action_count"]
            _insight_cache["pulse"]["action_count"] = prev_count + 1
            _insight_cache["pulse"]["generated"] = time.strftime("%Y-%m-%d %H:%M")
            # Clear pulse insights so next request regenerates
            _insight_cache["pulse"]["insights"] = []

        return jsonify({
            "status": "acknowledged",
            "event": event_type,
            "message": "Pulse insights will regenerate on next request"
        }), 200

    except Exception as e:
        logger.error(f"Pulse refresh error: {e}")
        return jsonify({"status": "error", "error": str(e)}), 500


@flask_app.route("/api/insights/refresh/cards", methods=["POST"])
def refresh_cards_insights():
    """
    Called by Supabase webhook when statements/transactions are uploaded.
    """
    try:
        payload = request.get_json(silent=True) or {}
        event_type = payload.get("type", "unknown")
        logger.info(f"Cards refresh triggered — event: {event_type}")

        with _cache_lock:
            _insight_cache["cards"]["insights"] = []
            _insight_cache["cards"]["generated"] = None

        return jsonify({
            "status": "acknowledged",
            "event": event_type,
            "message": "Cards insights cache cleared — will regenerate on next request"
        }), 200

    except Exception as e:
        logger.error(f"Cards refresh error: {e}")
        return jsonify({"status": "error", "error": str(e)}), 500


@flask_app.route("/api/insights/status", methods=["GET"])
def insights_status():
    """Debug endpoint — shows cache state for all modules."""
    with _cache_lock:
        status = {
            "charity": {
                "has_cache": len(_insight_cache["charity"]["insights"]) > 0,
                "generated": _insight_cache["charity"]["generated"],
                "row_count": _insight_cache["charity"]["row_count"],
            },
            "pulse": {
                "has_cache": len(_insight_cache["pulse"]["insights"]) > 0,
                "generated": _insight_cache["pulse"]["generated"],
            },
            "cards": {
                "has_cache": len(_insight_cache["cards"]["insights"]) > 0,
                "generated": _insight_cache["cards"]["generated"],
            },
        }
    r = jsonify(status)
    r.headers["Access-Control-Allow-Origin"] = "*"
    return r, 200


# ══════════════════════════════════════════════════════
# SUPABASE CONFIG — for Cards module
# ══════════════════════════════════════════════════════
SUPABASE_URL     = "https://flpzqovnyrlgnahnlimh.supabase.co"
SUPABASE_SERVICE = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImZscHpxb3ZueXJsZ25haG5saW1oIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc4MDU3NDAyNSwiZXhwIjoyMDk2MTUwMDI1fQ.00GDlzw34xVLn9tMaHye4SjNlt3mltfXLT0XdUHyMM8"
SB_HEADERS = {
    "apikey": SUPABASE_SERVICE,
    "Authorization": f"Bearer {SUPABASE_SERVICE}",
    "Content-Type": "application/json",
    "Prefer": "return=representation"
}

def sb_get(table, params=""):
    r = requests.get(f"{SUPABASE_URL}/rest/v1/{table}?{params}", headers=SB_HEADERS)
    return r.json() if r.ok else []

def sb_post(table, data):
    r = requests.post(f"{SUPABASE_URL}/rest/v1/{table}", headers=SB_HEADERS, json=data)
    return r.json() if r.ok else None

def sb_patch(table, match, data):
    r = requests.patch(f"{SUPABASE_URL}/rest/v1/{table}?{match}", headers=SB_HEADERS, json=data)
    return r.ok

# ══════════════════════════════════════════════════════
# CARDS API — Supabase-backed
# ══════════════════════════════════════════════════════

@flask_app.route("/api/cards/statements", methods=["GET"])
def api_cards_statements():
    """Return all statements, latest first — camelCase for React app."""
    try:
        stmts = sb_get("card_statements", "order=created_at.desc")
        if not isinstance(stmts, list):
            stmts = []
        # Map snake_case → camelCase to match existing React field names
        mapped = [{
            "id":              s.get("id"),
            "bank":            s.get("bank"),
            "last4":           s.get("last4"),
            "cardName":        s.get("card_name"),
            "statementMonth":  s.get("statement_month"),
            "closingBalance":  float(s.get("closing_balance") or 0),
            "paymentDueDate":  s.get("payment_due_date"),
            "minimumDue":      float(s.get("minimum_due") or 0),
            "totalSpend":      float(s.get("total_spend") or 0),
            "totalCredits":    float(s.get("total_credits") or 0),
        } for s in stmts]
        r = jsonify(mapped)
        r.headers["Access-Control-Allow-Origin"] = "*"
        return r, 200
    except Exception as e:
        r = jsonify({"error": str(e)})
        r.headers["Access-Control-Allow-Origin"] = "*"
        return r, 500

@flask_app.route("/api/cards/transactions", methods=["GET"])
def api_cards_transactions():
    """Return transactions with optional filters — camelCase for React app."""
    try:
        month    = request.args.get("month", "")
        bank     = request.args.get("bank", "")
        category = request.args.get("category", "")
        limit    = request.args.get("limit", "200")
        ey_only  = request.args.get("ey_only", "false")

        params = ["order=date.desc", f"limit={limit}"]
        if month:    params.append(f"statement_month=eq.{month}")
        if bank:     params.append(f"bank=eq.{bank}")
        if category: params.append(f"category=eq.{category}")
        if ey_only == "true": params.append("is_ey_reimbursable=eq.true")

        txns = sb_get("card_transactions", "&".join(params))
        if not isinstance(txns, list):
            txns = []
        # Map snake_case → camelCase to match existing React field names
        mapped = [{
            "id":              t.get("id"),
            "statementId":     t.get("statement_id"),
            "bank":            t.get("bank"),
            "card":            t.get("last4"),
            "statementMonth":  t.get("statement_month"),
            "date":            t.get("date"),
            "description":     t.get("description"),
            "amount":          float(t.get("amount") or 0),
            "category":        t.get("category", "Others"),
            "isEyReimbursable":t.get("is_ey_reimbursable", False),
        } for t in txns]
        r = jsonify(mapped)
        r.headers["Access-Control-Allow-Origin"] = "*"
        return r, 200
    except Exception as e:
        r = jsonify({"error": str(e)})
        r.headers["Access-Control-Allow-Origin"] = "*"
        return r, 500

@flask_app.route("/api/cards/upload", methods=["POST", "OPTIONS"])
def api_cards_upload():
    """
    Receive parsed statement data from the app after PDF processing.
    Saves statement + transactions to Supabase.
    Trigger then fires AI analysis automatically.
    """
    if request.method == "OPTIONS":
        r = jsonify({})
        r.headers["Access-Control-Allow-Origin"] = "*"
        r.headers["Access-Control-Allow-Headers"] = "*"
        return r, 200
    try:
        data          = request.get_json()
        bank          = data.get("bank", "")
        last4         = data.get("last4", "")
        card_name     = data.get("card_name", "")
        month         = data.get("statement_month", "")
        closing_bal   = data.get("closing_balance", 0)
        due_date      = data.get("payment_due_date", "")
        min_due       = data.get("minimum_due", 0)
        total_spend   = data.get("total_spend", 0)
        total_credits = data.get("total_credits", 0)
        transactions  = data.get("transactions", [])

        # Upsert statement
        stmt_data = {
            "bank": bank, "last4": last4, "card_name": card_name,
            "statement_month": month, "closing_balance": closing_bal,
            "payment_due_date": due_date, "minimum_due": min_due,
            "total_spend": total_spend, "total_credits": total_credits
        }
        # Use upsert
        headers_upsert = {**SB_HEADERS, "Prefer": "resolution=merge-duplicates,return=representation"}
        sr = requests.post(f"{SUPABASE_URL}/rest/v1/card_statements",
            headers=headers_upsert, json=stmt_data)
        if not sr.ok:
            return jsonify({"error": f"Statement save failed: {sr.text}"}), 500

        stmt_list = sr.json()
        stmt_id   = stmt_list[0]["id"] if isinstance(stmt_list, list) and stmt_list else None

        # Insert transactions in batch
        txn_rows = []
        for t in transactions:
            txn_rows.append({
                "statement_id":       stmt_id,
                "bank":               bank,
                "last4":              last4,
                "statement_month":    month,
                "date":               t.get("date"),
                "description":        t.get("description", ""),
                "amount":             t.get("amount", 0),
                "category":           t.get("category", "Others"),
                "is_ey_reimbursable": t.get("is_ey_reimbursable", False),
            })

        if txn_rows:
            tr = requests.post(f"{SUPABASE_URL}/rest/v1/card_transactions",
                headers={**SB_HEADERS, "Prefer": "return=minimal"},
                json=txn_rows)
            if not tr.ok:
                logger.error(f"Transactions save error: {tr.text}")

        r = jsonify({
            "success": True,
            "statement_id": stmt_id,
            "transactions_saved": len(txn_rows),
            "message": "Statement saved. AI analysis triggered automatically."
        })
        r.headers["Access-Control-Allow-Origin"] = "*"
        return r, 200

    except Exception as e:
        r = jsonify({"error": str(e)})
        r.headers["Access-Control-Allow-Origin"] = "*"
        return r, 500


@flask_app.route("/api/cards/analyse", methods=["POST", "OPTIONS"])
def api_cards_analyse():
    """
    Called automatically by Supabase trigger when a statement is uploaded.
    Runs Claude AI analysis and creates Pulse actions for:
    - Payment due alerts
    - EY reimbursable tracking
    - Spend spikes
    - Unusual transactions
    """
    if request.method == "OPTIONS":
        r = jsonify({})
        r.headers["Access-Control-Allow-Origin"] = "*"
        r.headers["Access-Control-Allow-Headers"] = "*"
        return r, 200
    try:
        data         = request.get_json(silent=True) or {}
        statement_id = data.get("statement_id")
        bank         = data.get("bank", "")
        month        = data.get("month", "")

        if not statement_id:
            return jsonify({"error": "No statement_id"}), 400

        logger.info(f"Cards AI analysis triggered — {bank} {month}")

        # Fetch statement
        stmts = sb_get("card_statements", f"id=eq.{statement_id}")
        if not stmts:
            return jsonify({"error": "Statement not found"}), 404
        stmt = stmts[0]

        # Fetch transactions for this statement
        txns = sb_get("card_transactions",
            f"statement_id=eq.{statement_id}&order=amount.desc")

        # Fetch previous month for comparison
        prev_txns = sb_get("card_transactions",
            f"bank=eq.{bank}&order=date.desc&limit=200")

        # Format for Claude
        today_str = time.strftime("%d %B %Y")
        txn_summary = []
        for t in txns[:50]:
            ey = " [EY]" if t.get("is_ey_reimbursable") else ""
            txn_summary.append(
                f"{t['date']} | {t['description'][:40]} | AED {t['amount']:.0f} | {t['category']}{ey}"
            )

        # Category totals
        cat_totals = {}
        for t in txns:
            if t["amount"] > 0:
                cat = t.get("category", "Others")
                cat_totals[cat] = cat_totals.get(cat, 0) + t["amount"]

        ey_total = sum(t["amount"] for t in txns if t.get("is_ey_reimbursable"))
        ey_count = sum(1 for t in txns if t.get("is_ey_reimbursable"))

        prompt = f"""You are a financial AI for Majid's Al Khalique app.
Today: {today_str}

Statement: {bank} ({stmt.get("last4","")}) — {month}
Closing Balance: AED {stmt.get("closing_balance",0):.0f}
Payment Due: {stmt.get("payment_due_date","N/A")}
Total Spend: AED {stmt.get("total_spend",0):.0f}

Category breakdown:
{chr(10).join(f"  {k}: AED {v:.0f}" for k,v in sorted(cat_totals.items(), key=lambda x:-x[1]))}

EY Reimbursable: AED {ey_total:.0f} across {ey_count} transactions

Top transactions:
{chr(10).join(txn_summary[:20])}

Generate Pulse ACTIONS for Majid. Each action should be something he needs to DO.

Rules:
- Payment due within 7 days → urgent action
- Payment due 8-14 days → high priority action
- EY reimbursable > AED 5,000 → high priority action to submit claim
- Any single transaction > AED 10,000 → action to verify/note
- Spend spike > 20% in any category vs typical → amber action

Return ONLY a JSON array of actions to create in Pulse:
[
  {{
    "title": "Pay Mashreq Solitaire AED 94.8K",
    "detail": "Payment due 4 Jun 2026. Closing balance AED 94,800. Avoid late fees.",
    "priority": "urgent",
    "category": "personal",
    "due_date": "2026-06-04",
    "action_type": "payment_due",
    "amount": 94800
  }}
]

Only include actions that are genuinely actionable. Max 5 actions. Empty array [] if nothing urgent."""

        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"): raw = raw[4:]
        actions_to_create = json.loads(raw.strip())

        created = []
        for action in actions_to_create:
            # Create in Pulse actions table
            pulse_action = {
                "title":    action.get("title", ""),
                "detail":   action.get("detail", ""),
                "priority": action.get("priority", "high"),
                "category": action.get("category", "personal"),
                "due_date": action.get("due_date"),
                "status":   "open",
                "source":   "cards_ai",
                "subject":  f"Cards — {bank} {month}"
            }
            result = sb_post("actions", pulse_action)
            if result and isinstance(result, list) and result[0].get("id"):
                action_id = result[0]["id"]
                # Log in card_ai_actions
                sb_post("card_ai_actions", {
                    "statement_id": statement_id,
                    "action_id":    action_id,
                    "action_type":  action.get("action_type", "general"),
                    "title":        action.get("title", ""),
                    "detail":       action.get("detail", ""),
                    "amount":       action.get("amount")
                })
                created.append(action_id)

        logger.info(f"Cards AI: {len(created)} Pulse actions created for {bank} {month}")

        # Update insight cache
        with _cache_lock:
            _insight_cache["cards"]["insights"] = []
            _insight_cache["cards"]["generated"] = None

        r = jsonify({
            "status":          "analysed",
            "actions_created": len(created),
            "bank":            bank,
            "month":           month
        })
        r.headers["Access-Control-Allow-Origin"] = "*"
        return r, 200

    except Exception as e:
        logger.error(f"Cards analyse error: {e}")
        r = jsonify({"error": str(e)})
        r.headers["Access-Control-Allow-Origin"] = "*"
        return r, 500


@flask_app.route("/api/cards/search", methods=["POST", "OPTIONS"])
def api_cards_search():
    """AI search over card transactions in Supabase."""
    if request.method == "OPTIONS":
        r = jsonify({})
        r.headers["Access-Control-Allow-Origin"] = "*"
        r.headers["Access-Control-Allow-Headers"] = "*"
        return r, 200
    try:
        data  = request.get_json()
        query = data.get("query", "").strip()
        month = data.get("month", "")
        bank  = data.get("bank", "")

        if not query:
            return jsonify({"error": "No query"}), 400

        # Fetch relevant transactions
        params = ["order=date.desc", "limit=200"]
        if month: params.append(f"statement_month=eq.{month}")
        if bank:  params.append(f"bank=eq.{bank}")
        txns = sb_get("card_transactions", "&".join(params))

        if not txns:
            r = jsonify({"answer": "No transactions found.", "transactions": []})
            r.headers["Access-Control-Allow-Origin"] = "*"
            return r, 200

        txn_lines = [
            f"[{i}] {t['date']} | {t['description']} | AED {t['amount']:.0f} | {t['category']} | {t['bank']} {t['last4']}"
            for i, t in enumerate(txns)
        ]

        prompt = f"""You are a card transaction search assistant for Majid.
Today: {time.strftime("%d %B %Y")}

Transactions (newest first):
{chr(10).join(txn_lines[:100])}

Query: "{query}"

Return ONLY JSON:
{{
  "answer": "1-2 sentence answer",
  "txn_indices": [0, 3, 7]
}}

txn_indices = [index] numbers matching the query, max 15, most relevant first."""

        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"): raw = raw[4:]
        result       = json.loads(raw.strip())
        answer       = result.get("answer", "")
        txn_indices  = result.get("txn_indices", [])
        matched_txns = [txns[i] for i in txn_indices if 0 <= i < len(txns)]

        r = jsonify({"answer": answer, "transactions": matched_txns})
        r.headers["Access-Control-Allow-Origin"] = "*"
        return r, 200

    except Exception as e:
        r = jsonify({"error": str(e), "answer": "Search failed.", "transactions": []})
        r.headers["Access-Control-Allow-Origin"] = "*"
        return r, 500



@flask_app.route("/api/cards/migrate", methods=["POST", "GET"])
def api_cards_migrate():
    """
    One-time migration from Google Sheets (via Node API) to Supabase.
    Reads /api/statements and /api/transactions from the Node server.
    """
    try:
        import urllib.request as ureq

        NODE_BASE = "https://ams-finance-production.up.railway.app"

        # ── Fetch statements ──
        try:
            req = ureq.Request(f"{NODE_BASE}/api/statements")
            with ureq.urlopen(req, timeout=20) as resp:
                statements = json.loads(resp.read().decode())
        except Exception as e:
            return jsonify({"error": f"Cannot fetch statements: {e}"}), 500

        if not isinstance(statements, list):
            return jsonify({"error": "Statements not a list", "data": str(statements)[:200]}), 500

        # ── Fetch all transactions ──
        try:
            req = ureq.Request(f"{NODE_BASE}/api/transactions")
            with ureq.urlopen(req, timeout=30) as resp:
                all_txns = json.loads(resp.read().decode())
        except Exception as e:
            all_txns = []
            logger.warning(f"Could not fetch transactions: {e}")

        # Index transactions by statementId
        txns_by_stmt = {}
        for t in (all_txns if isinstance(all_txns, list) else []):
            sid = t.get("statementId", "")
            if sid not in txns_by_stmt:
                txns_by_stmt[sid] = []
            txns_by_stmt[sid].append(t)

        migrated_stmts = 0
        migrated_txns  = 0
        errors         = []

        upsert_headers = {**SB_HEADERS, "Prefer": "resolution=merge-duplicates,return=representation"}

        for stmt in statements:
            try:
                bank         = stmt.get("bank", "")
                stmt_id_node = stmt.get("statementId", "")
                month        = stmt.get("statementMonth", "")
                due_date     = stmt.get("paymentDueDate", "")
                closing_bal  = float(stmt.get("closingBalance", 0) or 0)

                # Derive last4 from bank
                last4 = "7844" if "Mashreq" in bank else "6971" if "EIB" in bank else ""
                card_name = "Solitaire" if "Mashreq" in bank else "Skywards" if "EIB" in bank else ""

                if not bank or not month:
                    errors.append(f"Skipped: missing bank/month — {stmt}")
                    continue

                stmt_data = {
                    "bank":             bank,
                    "last4":            last4,
                    "card_name":        card_name,
                    "statement_month":  month,
                    "closing_balance":  closing_bal,
                    "payment_due_date": due_date,
                    "minimum_due":      0,
                    "total_spend":      closing_bal,
                    "total_credits":    0,
                }

                sr = requests.post(f"{SUPABASE_URL}/rest/v1/card_statements",
                    headers=upsert_headers, json=stmt_data)

                if not sr.ok:
                    errors.append(f"Statement failed {bank} {month}: {sr.text[:100]}")
                    continue

                result  = sr.json()
                sb_stmt_id = result[0]["id"] if isinstance(result, list) and result else None
                migrated_stmts += 1

                # Insert transactions for this statement
                stmt_txns = txns_by_stmt.get(stmt_id_node, [])
                txn_rows  = []

                for t in stmt_txns:
                    try:
                        date_val = t.get("date", "")
                        # Convert "15 May 2026" → "2026-05-15"
                        if date_val:
                            import datetime
                            month_map = {"Jan":"01","Feb":"02","Mar":"03","Apr":"04","May":"05","Jun":"06",
                                        "Jul":"07","Aug":"08","Sep":"09","Oct":"10","Nov":"11","Dec":"12"}
                            parts = str(date_val).split(" ")
                            if len(parts) == 3:
                                date_val = f"{parts[2]}-{month_map.get(parts[1],'01')}-{parts[0].zfill(2)}"

                        desc = t.get("description", "")
                        amt  = float(t.get("amount", 0) or 0)
                        cat  = t.get("category", "Others") or "Others"
                        is_ey = "EY" in cat or "reimburse" in cat.lower()

                        txn_rows.append({
                            "statement_id":       sb_stmt_id,
                            "bank":               t.get("bank", bank),
                            "last4":              last4,
                            "statement_month":    t.get("statementMonth", month),
                            "date":               str(date_val)[:10] if date_val else None,
                            "description":        desc,
                            "amount":             amt,
                            "category":           cat,
                            "is_ey_reimbursable": is_ey,
                        })
                    except Exception as te:
                        errors.append(f"Txn error: {te}")

                if txn_rows:
                    tr = requests.post(f"{SUPABASE_URL}/rest/v1/card_transactions",
                        headers={**SB_HEADERS, "Prefer": "return=minimal"},
                        json=txn_rows)
                    if tr.ok:
                        migrated_txns += len(txn_rows)
                    else:
                        errors.append(f"Txns failed {bank} {month}: {tr.text[:100]}")

            except Exception as se:
                errors.append(f"Statement error: {se}")

        r = jsonify({
            "status":           "migration_complete",
            "statements":       migrated_stmts,
            "transactions":     migrated_txns,
            "errors":           errors[:10],
            "total_statements": len(statements),
            "total_txns_found": len(all_txns) if isinstance(all_txns, list) else 0,
        })
        r.headers["Access-Control-Allow-Origin"] = "*"
        return r, 200

    except Exception as e:
        r = jsonify({"error": str(e)})
        r.headers["Access-Control-Allow-Origin"] = "*"
        return r, 500

@flask_app.route("/api/cards/migrate_OLD", methods=["POST", "GET"])
def api_cards_migrate_old():
    """
    One-time migration: reads existing statements from Node API
    and writes them to Supabase card_statements + card_transactions.
    Call once via: GET /api/cards/migrate
    """
    try:
        import urllib.request as ureq

        NODE_BASE = "http://localhost:3001"  # Node API running alongside

        # Try to read existing statements from Node server
        try:
            with ureq.urlopen(f"{NODE_BASE}/api/statements", timeout=10) as resp:
                statements = json.loads(resp.read().decode())
        except Exception as e:
            # Try the Railway app URL as fallback
            try:
                req = ureq.Request("https://ams-finance-production.up.railway.app/api/statements")
                with ureq.urlopen(req, timeout=15) as resp:
                    statements = json.loads(resp.read().decode())
            except Exception as e2:
                return jsonify({"error": f"Cannot reach statements API: {e} / {e2}"}), 500

        if not isinstance(statements, list):
            return jsonify({"error": "Statements not a list", "data": str(statements)[:200]}), 500

        migrated_stmts = 0
        migrated_txns  = 0
        errors         = []

        for stmt in statements:
            try:
                bank  = stmt.get("bank", "")
                last4 = stmt.get("last4", "") or stmt.get("cardLast4", "")
                month = stmt.get("statementMonth", "") or stmt.get("statement_month", "")

                if not bank or not month:
                    errors.append(f"Skipped: missing bank/month in {stmt}")
                    continue

                stmt_data = {
                    "bank":              bank,
                    "last4":             last4,
                    "card_name":         stmt.get("cardName", "") or stmt.get("card_name", ""),
                    "statement_month":   month,
                    "closing_balance":   float(stmt.get("closingBalance", 0) or stmt.get("closing_balance", 0) or 0),
                    "payment_due_date":  stmt.get("paymentDueDate", "") or stmt.get("payment_due_date", ""),
                    "minimum_due":       float(stmt.get("minimumDue", 0) or stmt.get("minimum_due", 0) or 0),
                    "total_spend":       float(stmt.get("totalSpend", 0) or stmt.get("total_spend", 0) or 0),
                    "total_credits":     float(stmt.get("totalCredits", 0) or stmt.get("total_credits", 0) or 0),
                }

                # Upsert statement
                upsert_headers = {**SB_HEADERS, "Prefer": "resolution=merge-duplicates,return=representation"}
                sr = requests.post(f"{SUPABASE_URL}/rest/v1/card_statements",
                    headers=upsert_headers, json=stmt_data)

                if not sr.ok:
                    errors.append(f"Statement upsert failed {bank} {month}: {sr.text[:100]}")
                    continue

                stmt_result = sr.json()
                stmt_id = stmt_result[0]["id"] if isinstance(stmt_result, list) and stmt_result else None
                migrated_stmts += 1

                # Get transactions for this statement
                transactions = stmt.get("transactions", [])
                if not transactions:
                    # Try fetching from transactions endpoint
                    try:
                        tx_url = f"https://ams-finance-production.up.railway.app/api/transactions?month={month}&bank={bank}"
                        req = ureq.Request(tx_url)
                        with ureq.urlopen(req, timeout=15) as resp:
                            tx_data = json.loads(resp.read().decode())
                            transactions = tx_data if isinstance(tx_data, list) else tx_data.get("transactions", [])
                    except:
                        transactions = []

                txn_rows = []
                for t in transactions:
                    try:
                        date_val = t.get("date", "") or t.get("txnDate", "")
                        # Normalise date to YYYY-MM-DD
                        if date_val and "/" in str(date_val):
                            parts = str(date_val).split("/")
                            if len(parts) == 3:
                                date_val = f"{parts[2]}-{parts[1].zfill(2)}-{parts[0].zfill(2)}"

                        amount = float(str(t.get("amount", 0) or t.get("debit", 0) or 0).replace(",",""))

                        txn_rows.append({
                            "statement_id":       stmt_id,
                            "bank":               bank,
                            "last4":              last4,
                            "statement_month":    month,
                            "date":               str(date_val)[:10] if date_val else None,
                            "description":        str(t.get("description", t.get("narration", t.get("details", "")))),
                            "amount":             amount,
                            "category":           t.get("category", "Others") or "Others",
                            "is_ey_reimbursable": bool(t.get("isEyReimbursable", t.get("is_ey_reimbursable", False))),
                        })
                    except Exception as te:
                        errors.append(f"Txn parse error: {te}")

                if txn_rows:
                    tr = requests.post(f"{SUPABASE_URL}/rest/v1/card_transactions",
                        headers={**SB_HEADERS, "Prefer": "return=minimal"},
                        json=txn_rows)
                    if tr.ok:
                        migrated_txns += len(txn_rows)
                    else:
                        errors.append(f"Txns insert failed {bank} {month}: {tr.text[:100]}")

            except Exception as se:
                errors.append(f"Statement error: {se}")

        r = jsonify({
            "status":           "migration_complete",
            "statements":       migrated_stmts,
            "transactions":     migrated_txns,
            "errors":           errors[:10],
            "total_statements": len(statements)
        })
        r.headers["Access-Control-Allow-Origin"] = "*"
        return r, 200

    except Exception as e:
        r = jsonify({"error": str(e)})
        r.headers["Access-Control-Allow-Origin"] = "*"
        return r, 500


def run_flask():
    port=int(os.environ.get("PORT",8080))
    flask_app.run(host="0.0.0.0",port=port,debug=False,use_reloader=False)

def main():
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    logger.info("Flask API started")
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("balances", balances_cmd))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    logger.info("Telegram bot started...")
    app.run_polling()

if __name__ == "__main__":
    main()



