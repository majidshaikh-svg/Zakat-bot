import os, json, logging, tempfile, base64, urllib.request, time, re, io, threading
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
    url = SCRIPT_URL + "?t=" + str(int(time.time()))
    with urllib.request.urlopen(url, timeout=15) as r:
        rows = json.loads(r.read().decode())
    bal = {"Zakat": 0, "Khair": 0, "Asanee": 0}
    try: bal["Khair"]  = float(str(rows[4][12]).replace(",","").replace(" ",""))
    except: pass
    try: bal["Zakat"]  = float(str(rows[4][17]).replace(",","").replace(" ",""))
    except: pass
    try: bal["Asanee"] = float(str(rows[4][22]).replace(",","").replace(" ",""))
    except: pass
    return bal

def get_rows():
    url = SCRIPT_URL + "?t=" + str(int(time.time()))
    with urllib.request.urlopen(url, timeout=15) as r:
        return json.loads(r.read().decode())

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

client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)

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

def extract(text, img_b64=None, recent=""):
    content = []
    if img_b64:
        content.append({"type":"image","source":{"type":"base64","media_type":"image/jpeg","data":img_b64}})
    content.append({"type":"text","text": text or "See attached."})
    today = time.strftime("%d-%b-%y")
    system = f"""Extract ALL charity payment entries. Categories: Zakat, Khair, Asanee.
Return ONLY a JSON array:
[{{"date":"19-Apr-26","amount":50000,"category":"Zakat","details":"Mama Raja - covers Jan 2026"}}]
If nothing found: [{{"error":"reason"}}]
Rules:
- Amount in PKR. 1m=1000000, 1 lakh=100000, 1k=1000
- Date column = today ({today}) — the date this entry is being recorded
- Details MUST include the coverage period if mentioned (e.g. "Dr Malla zakat - covers Dec 2025 to Feb 2026")
- If user mentions a period/month/year in their message, always include it in details
- Fix spelling mistakes in category names
- Details should be clean human-readable description. Do NOT repeat the amount or category.
Recent entries:
{recent}"""
    r = client.messages.create(model="claude-sonnet-4-20250514", max_tokens=1000, system=system, messages=[{"role":"user","content":content}])
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
        r = client.messages.create(model="claude-sonnet-4-20250514", max_tokens=300,
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
            display_date = date if date.strip() else time.strftime("%d-%b-%y")
            data_rows.append(f"{display_date} | {head} | PKR {amount} | {cat} | {details}")

        last_100 = data_rows[-100:] if len(data_rows) > 100 else data_rows

        if not last_100:
            r = jsonify({"insights": [], "error": "No transactions found"})
            r.headers["Access-Control-Allow-Origin"] = "*"
            return r, 200

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
            model="claude-sonnet-4-20250514",
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"): raw = raw[4:]
        raw = raw.strip()
        insights = json.loads(raw)
        r = jsonify({"insights": insights, "generated": time.strftime("%Y-%m-%d %H:%M")})
        r.headers["Access-Control-Allow-Origin"] = "*"
        return r, 200
    except Exception as e:
        r = jsonify({"error": str(e), "insights": []})
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
