import os, json, logging, tempfile, base64, urllib.request, time, re
import anthropic
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN  = os.environ["TELEGRAM_TOKEN"]
CLAUDE_API_KEY  = os.environ["CLAUDE_API_KEY"]
ALLOWED_USER_ID = int(os.environ.get("ALLOWED_USER_ID", "0"))
SCRIPT_URL      = "https://script.google.com/macros/s/AKfycbzzE1itHnJ87R_ffxE5ZcRYth0Ds0_OOj46XGGjvW0gAi7CiE47L4ruTehZrefNY7uD/exec"
CATEGORIES      = ["Zakat", "Khair", "Asanee"]

CAT_ICON = {
    "Zakat":  "🕌",
    "Khair":  "🤲",
    "Asanee": "👨‍👩‍👧",
}

DIVIDER = "━━━━━━━━━━━━━━━━━━━━"

def get_balances():
    url = SCRIPT_URL + "?t=" + str(int(time.time()))
    with urllib.request.urlopen(url, timeout=15) as r:
        rows = json.loads(r.read().decode())
    bal = {"Zakat": 0, "Khair": 0, "Asanee": 0}
    try: bal["Khair"]  = float(str(rows[4][10]).replace(",","").replace(" ",""))
    except: pass
    try: bal["Zakat"]  = float(str(rows[4][15]).replace(",","").replace(" ",""))
    except: pass
    try: bal["Asanee"] = float(str(rows[4][20]).replace(",","").replace(" ",""))
    except: pass
    return bal

def get_rows():
    url = SCRIPT_URL + "?t=" + str(int(time.time()))
    with urllib.request.urlopen(url, timeout=15) as r:
        return json.loads(r.read().decode())

def append_entry(date, amount, category, details):
    data = json.dumps(["", amount, "", category, details]).encode()
    req = urllib.request.Request(SCRIPT_URL, data=data, method="POST")
    req.add_header("Content-Type", "text/plain")
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())

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
    """Normalize date display - strip raw ISO timestamps."""
    if not date_str:
        return "—"
    # Strip ISO timestamps like 2026-12-23T20:00:00.000Z
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
    """Remove amount/category repetition from details field."""
    if not details:
        return details
    amt_str = str(int(amount))
    cleaned = details
    # Remove leading patterns like "Is khair PKR 100,000"
    patterns = [
        rf"(?i)^is\s+{re.escape(category)}\s+pkr\s+[\d,]+\s*",
        rf"(?i)^is\s+[\d,]+\s*",
        rf"(?i)pkr\s+{re.escape(amt_str)}\s*",
    ]
    for p in patterns:
        cleaned = re.sub(p, "", cleaned).strip()
    # Remove trailing = amount patterns like "ramzan sahree =500000"
    cleaned = re.sub(r'\s*[=\-]\s*[\d,]+\s*$', '', cleaned).strip()
    # Remove leading/trailing punctuation
    cleaned = cleaned.strip(" -=|,")
    return cleaned if cleaned else details

def format_entry_list(results, cat_filter=None):
    total = sum(e["amount"] for e in results)
    label = cat_filter or "All"
    msg = f"📋 Last {len(results)} {label} Entries\n{DIVIDER}\n"
    for i, e in enumerate(results):
        icon = CAT_ICON.get(e["category"], "💰")
        date_str = format_date_display(e["date"]) if e["date"] else "—"
        details = clean_details(e.get("details",""), e.get("amount",0), e.get("category",""))
        msg += f"{i+1}. {icon} {e['category']} | PKR {fmt(e['amount'])} | 📅 {date_str}\n"
        if details:
            msg += f"   📝 {details}\n"
        msg += "\n"
    msg += f"{DIVIDER}\n💵 Total: PKR {fmt(total)}"
    return msg

def format_pending(entries):
    msg = ""
    for i, e in enumerate(entries):
        icon = CAT_ICON.get(e["category"], "💰")
        date_str = format_date_display(e["date"]) if e["date"] else "—"
        details = clean_details(e.get("details",""), e.get("amount",0), e.get("category",""))
        msg += f"{i+1}. {icon} {e['category']} | PKR {fmt(e['amount'])} | 📅 {date_str}\n"
        if details:
            msg += f"   📝 {details}\n"
        msg += "\n"
    return msg

def check_duplicates(entries, rows):
    """Check last 20 entries for duplicates by amount + category + similar description."""
    dup_found = []
    recent_rows = [r for r in rows[-20:] if len(r) >= 5]
    for row in recent_rows:
        row_cat    = str(row[3]).strip()
        row_det    = str(row[4]).strip()
        row_date   = str(row[0]).strip()
        try: row_amt = float(str(row[1]).replace(",",""))
        except: continue
        for entry in entries:
            same_amount   = int(row_amt) == int(entry.get("amount", -1))
            same_category = row_cat.lower() == entry.get("category","").lower()
            det = entry.get("details","")
            similar_desc  = (
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

def apply_corrections(entries, text):
    """
    Parse inline corrections before YES/NO confirmation.
    Supports:
      - "remove 1" or "delete 2"
      - "1 is not a transaction"
      - "4 is 182500" — correct amount of entry 4
    Returns (updated_entries, correction_summary).
    """
    corrections = []
    to_remove = set()
    tl = text.lower().strip()

    # "remove N" or "delete N"
    for m in re.finditer(r'(?:remove|delete)\s+(\d+)', tl):
        idx = int(m.group(1)) - 1
        if 0 <= idx < len(entries):
            to_remove.add(idx)
            corrections.append(f"Removed entry {idx+1}")

    # "N is not a transaction"
    for m in re.finditer(r'(\d+)\s+is\s+not\s+a\s+transaction', tl):
        idx = int(m.group(1)) - 1
        if 0 <= idx < len(entries):
            to_remove.add(idx)
            corrections.append(f"Removed entry {idx+1}")

    # "N is AMOUNT" — correct amount
    for m in re.finditer(r'(\d+)\s+is\s+([\d,]+)', text):
        idx = int(m.group(1)) - 1
        amt_str = m.group(2).replace(",","")
        if amt_str.isdigit() and 0 <= idx < len(entries) and idx not in to_remove:
            old = entries[idx]["amount"]
            entries[idx]["amount"] = int(amt_str)
            corrections.append(f"Entry {idx+1}: PKR {fmt(old)} → PKR {fmt(int(amt_str))}")

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
[{{"date":"19-Apr-26","amount":50000,"category":"Zakat","details":"Mama Raja"}}]
If nothing found: [{{"error":"reason"}}]
Rules:
- Amount in PKR. 1m=1000000, 1 lakh=100000, 1k=1000
- Date format DD-Mon-YY e.g. 19-Apr-26. If no date mentioned, use today: {today}
- Fix spelling mistakes in category names
- Details should be a clean human-readable description only. Do NOT repeat the amount or category name in details.
- Keep details concise and meaningful
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

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        bal = get_balances()
        msg = (
            f"🌙 Majid Charity Tracker\n"
            f"{DIVIDER}\n"
            f"💳 Balances:\n"
            f"{format_balances(bal)}\n"
            f"{DIVIDER}\n"
            f"📩 Send text, voice or screenshot!"
        )
        await update.message.reply_text(msg)
    except Exception as e:
        await update.message.reply_text(f"Bot running! Sheet error: {e}")

async def balances_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USER_ID and update.effective_user.id != ALLOWED_USER_ID: return
    try:
        bal = get_balances()
        msg = (
            f"💳 Balances\n"
            f"{DIVIDER}\n"
            f"{format_balances(bal)}"
        )
        await update.message.reply_text(msg)
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")

async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USER_ID and update.effective_user.id != ALLOWED_USER_ID: return
    text = update.message.text.strip()
    tl = text.lower()
    pending = ctx.user_data.get("pending", [])

    if tl in ["yes","y","confirm","ok"]:
        if pending:
            try:
                for entry in pending:
                    details = clean_details(entry.get("details",""), entry.get("amount",0), entry.get("category",""))
                    append_entry(entry["date"], entry["amount"], entry["category"], details)
                new = get_balances()
                ctx.user_data["pending"] = []
                msg = (
                    f"✅ Saved!\n"
                    f"{DIVIDER}\n"
                    f"💳 New Balances:\n"
                    f"{format_balances(new)}"
                )
                await update.message.reply_text(msg)
            except Exception as e:
                await update.message.reply_text(f"Error saving: {e}")
        else:
            await update.message.reply_text("No pending entry.")
        return

    if tl in ["no","cancel"]:
        ctx.user_data["pending"] = []
        await update.message.reply_text("❌ Cancelled.")
        return

    # --- Inline corrections while there are pending entries ---
    if pending and re.search(r'(\d+\s+is\s+|\bremove\s+\d+|\bdelete\s+\d+)', tl):
        updated, corrections = apply_corrections(list(pending), text)
        if corrections:
            ctx.user_data["pending"] = updated
            if not updated:
                ctx.user_data["pending"] = []
                await update.message.reply_text("✅ All entries removed. Nothing to save.")
                return
            try:
                bal = get_balances()
            except:
                bal = {}
            summary = "\n".join(f"  • {c}" for c in corrections)
            msg = (
                f"✏️ Updated:\n{summary}\n\n"
                f"✅ {len(updated)} entr{'y' if len(updated)==1 else 'ies'} remaining:\n\n"
                f"{format_pending(updated)}"
                f"{DIVIDER}\n"
                f"💳 Current Balances:\n"
                f"{format_balances(bal)}\n"
                f"{DIVIDER}\n"
                f"Reply YES to confirm or NO to cancel"
            )
            await update.message.reply_text(msg)
            return

    # Search queries
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
        for trigger in ["mentioning","mention","with","about","for","madiha","raja","khuda"]:
            if trigger in tl:
                parts = tl.split(trigger)
                if len(parts) > 1:
                    keyword = parts[-1].strip().split()[0] if parts[-1].strip() else None
                break
        m = re.search(r'mention(?:ing)?\s+(\w+)', tl)
        if m: keyword = m.group(1)

        results = []
        for row in rows[1:]:
            if len(row) < 4: continue
            date    = str(row[0]).strip()
            amount  = str(row[1]).strip()
            cat     = str(row[3]).strip() if len(row) > 3 else ""
            details = str(row[4]).strip() if len(row) > 4 else ""
            if cat not in CATEGORIES: continue
            if cat_filter and cat.lower() != cat_filter.lower(): continue
            if keyword and keyword.lower() not in details.lower() and keyword.lower() not in date.lower(): continue
            try: amt = float(str(amount).replace(",",""))
            except: amt = 0
            results.append({"date":date,"amount":amt,"category":cat,"details":details})
        results = results[-n:]
        results.reverse()
        if not results:
            await update.message.reply_text("No entries found.")
            return
        await update.message.reply_text(format_entry_list(results, cat_filter))
        return

    await update.message.reply_text("🔍 Analyzing...")
    try:
        rows = get_rows()
        recent = "\n".join([f"{r[0]}|{r[1]}|{r[3]}|{r[4]}" for r in rows[-10:] if len(r)>=5])
    except: recent = ""; rows = []
    try:
        entries = extract(text, recent=recent)
        if not entries or "error" in entries[0]:
            err = entries[0].get("error","unknown") if entries else "unknown"
            await update.message.reply_text(f"Could not extract: {err}\n\nTry again.")
            return

        dup_found = check_duplicates(entries, rows)
        ctx.user_data["pending"] = entries
        bal = get_balances()
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
        msg += "Reply YES to confirm or NO to cancel"
        await update.message.reply_text(msg)
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
            with open(tmp.name,"rb") as f: img_b64 = base64.b64encode(f.read()).decode()
        try:
            rows = get_rows()
            recent = "\n".join([f"{r[0]}|{r[1]}|{r[2]}|{r[3]}" for r in rows[-10:] if len(r)>=4])
        except: recent = ""; rows = []
        entries = extract(update.message.caption or "", img_b64=img_b64, recent=recent)
        if not entries or "error" in entries[0]:
            await update.message.reply_text("Could not extract. Add a caption.")
            return

        dup_found = check_duplicates(entries, rows)
        ctx.user_data["pending"] = entries
        bal = get_balances()
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
        msg += "Reply YES to confirm or NO to cancel"
        await update.message.reply_text(msg)
    except Exception as e:
        await update.message.reply_text(f"Photo error: {e}")

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("balances", balances_cmd))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    logger.info("Bot started...")
    app.run_polling()

if __name__ == "__main__":
    main()
