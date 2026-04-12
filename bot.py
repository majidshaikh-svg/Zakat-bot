import os, json, logging, tempfile, base64, urllib.request, time, re
import anthropic
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes, CommandHandler

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN  = os.environ["TELEGRAM_TOKEN"]
CLAUDE_API_KEY  = os.environ["CLAUDE_API_KEY"]
ALLOWED_USER_ID = int(os.environ.get("ALLOWED_USER_ID", "0"))
SCRIPT_URL      = "https://script.google.com/macros/s/AKfycbzzE1itHnJ87R_ffxE5ZcRYth0Ds0_OOj46XGGjvW0gAi7CiE47L4ruTehZrefNY7uD/exec"
CATEGORIES      = ["Zakat", "Khair", "Asanee"]

CAT_MAP = {
    "zakat":"Zakat","zakt":"Zakat","zakaat":"Zakat",
    "khair":"Khair","khiur":"Khair","kher":"Khair","hair":"Khair",
    "asanee":"Asanee","aasanee":"Asanee","asani":"Asanee","aasani":"Asanee","asane":"Asanee"
}

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
    data = json.dumps([date, amount, "", category, details]).encode()
    req = urllib.request.Request(SCRIPT_URL, data=data, method="POST")
    req.add_header("Content-Type", "text/plain")
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())

client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)

def fmt(n):
    try: return f"{float(str(n).replace(',','')):,.0f}"
    except: return str(n)

def format_balances(bal):
    return "\n".join([f"  {c}: {fmt(bal.get(c,0))} PKR" for c in CATEGORIES])

def format_pending(entries):
    msg = ""
    for i, e in enumerate(entries):
        msg += f"*{i+1}.* {e['date']} | {e['category']} | {fmt(e['amount'])} PKR | {e['details']}\n"
    return msg

def extract(text, img_b64=None, recent=""):
    content = []
    if img_b64:
        content.append({"type":"image","source":{"type":"base64","media_type":"image/jpeg","data":img_b64}})
    content.append({"type":"text","text": text or "See attached."})
    system = f"""Extract ALL charity payment entries from the input. Categories: Zakat, Khair, Asanee.
Return ONLY a JSON array:
[{{"date":"Apr-26","amount":50000,"category":"Zakat","details":"Mama Raja"}}]
If nothing found: [{{"error":"reason"}}]
Rules:
- Amount in PKR. 1m=1000000, 1 lakh=100000, 1k=1000
- Date format Mon-YY e.g. Apr-26. If no date use current month/year Apr-26
- Category MUST be exactly "Zakat", "Khair", or "Asanee" - fix spelling mistakes. Use "Asanee" not "Aasanee"
- Details: concise description
Recent entries:
{recent}"""
    r = client.messages.create(model="claude-sonnet-4-20250514", max_tokens=1000, system=system, messages=[{"role":"user","content":content}])
    raw = r.content[0].text.strip().replace("```json","").replace("```","").strip()
    result = json.loads(raw)
    if isinstance(result, dict): result = [result]
    return result

def search_entries(rows, keyword=None, category=None, month=None, limit=None):
    results = []
    for row in rows[1:]:
        if len(row) < 4: continue
        date    = str(row[0]).strip()
        amount  = str(row[1]).strip()
        cat     = str(row[3]).strip() if len(row) > 3 else ""
        details = str(row[4]).strip() if len(row) > 4 else ""
        if cat not in CATEGORIES and cat not in ["Aasanee"]: continue
        if cat == "Aasanee": cat = "Asanee"
        if category and cat.lower() != category.lower(): continue
        if keyword and keyword.lower() not in details.lower() and keyword.lower() not in date.lower(): continue
        if month and month.lower() not in date.lower(): continue
        try: amt = float(str(amount).replace(",",""))
        except: amt = 0
        results.append({"date":date,"amount":amt,"category":cat,"details":details})
    if limit: results = results[-limit:]
    return list(reversed(results))

def format_entries(entries, title):
    if not entries: return f"*{title}*\n\nNo entries found."
    total = sum(e["amount"] for e in entries)
    msg = f"*{title}*\n_{len(entries)} entries | Total: {fmt(total)} PKR_\n\n"
    for e in entries:
        msg += f"- {e['date']} | {fmt(e['amount'])} PKR | {e['category']} | {e['details']}\n"
    return msg

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        bal = get_balances()
        msg = f"Majid Charity Tracker\n\nBalances:\n{format_balances(bal)}\n\n"
        msg += "Send text, voice or screenshot!\n\n"
        msg += "Search commands:\n"
        msg += "- last 10 zakat\n- last 5 khair\n- search madiha\n- show december entries\n- /balances"
        await update.message.reply_text(msg)
    except Exception as e:
        await update.message.reply_text(f"Bot running! Sheet error: {e}")

async def balances_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USER_ID and update.effective_user.id != ALLOWED_USER_ID: return
    try:
        bal = get_balances()
        await update.message.reply_text(f"Current Balances:\n\n{format_balances(bal)}")
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")

async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USER_ID and update.effective_user.id != ALLOWED_USER_ID: return
    text = update.message.text.strip()
    tl = text.lower()
    pending = ctx.user_data.get("pending", [])

    # Handle entry corrections when there are pending entries
    if pending and isinstance(pending, list):
        # Category correction: "entry 2 is zakat" or "2 khair" or "entry 2 zakat"
        m = re.search(r'(?:entry\s*)?(\d+)\s*(?:is\s+|category\s+)?(\w+)', tl)
        if m:
            idx = int(m.group(1)) - 1
            raw_cat = m.group(2).strip().lower()
            new_cat = CAT_MAP.get(raw_cat)
            if 0 <= idx < len(pending) and new_cat:
                pending[idx]["category"] = new_cat
                ctx.user_data["pending"] = pending
                msg = "Updated! Here are your entries:\n\n"
                msg += format_pending(pending)
                msg += "\nReply YES to confirm all or make more corrections."
                await update.message.reply_text(msg)
                return

        # Amount correction: "entry 2 amount 500000" or "entry 2 amount 5m"
        m2 = re.search(r'(?:entry\s*)?(\d+)\s+amount\s+(\d+\.?\d*)\s*(m|k|l)?', tl)
        if m2:
            idx = int(m2.group(1)) - 1
            amt = float(m2.group(2))
            suffix = m2.group(3) or ""
            if suffix == "m": amt *= 1000000
            elif suffix == "k": amt *= 1000
            elif suffix == "l": amt *= 100000
            if 0 <= idx < len(pending):
                pending[idx]["amount"] = amt
                ctx.user_data["pending"] = pending
                msg = "Updated! Here are your entries:\n\n"
                msg += format_pending(pending)
                msg += "\nReply YES to confirm all or make more corrections."
                await update.message.reply_text(msg)
                return

        # Date correction: "entry 2 date jan-26"
        m3 = re.search(r'(?:entry\s*)?(\d+)\s+date\s+([a-z]{3}-\d{2})', tl)
        if m3:
            idx = int(m3.group(1)) - 1
            new_date = m3.group(2).capitalize()
            if 0 <= idx < len(pending):
                pending[idx]["date"] = new_date
                ctx.user_data["pending"] = pending
                msg = "Updated! Here are your entries:\n\n"
                msg += format_pending(pending)
                msg += "\nReply YES to confirm all or make more corrections."
                await update.message.reply_text(msg)
                return

    # YES confirm
    if tl in ["yes","y","confirm","ok"]:
        if pending:
            try:
                old = get_balances()
                for entry in pending:
                    append_entry(entry["date"], entry["amount"], entry["category"], entry["details"])
                new = get_balances()
                ctx.user_data["pending"] = []
                count = len(pending)
                msg = f"{count} entr{'y' if count==1 else 'ies'} saved!\n\nNew balances:\n{format_balances(new)}"
                await update.message.reply_text(msg)
            except Exception as e:
                await update.message.reply_text(f"Error saving: {e}")
        else:
            await update.message.reply_text("No pending entry.")
        return

    # NO cancel
    if tl in ["no","cancel"]:
        ctx.user_data["pending"] = []
        await update.message.reply_text("Cancelled.")
        return

    # Reporting queries
    months = {"january":"jan","february":"feb","march":"mar","april":"apr","may":"may",
              "june":"jun","july":"jul","august":"aug","september":"sep","october":"oct",
              "november":"nov","december":"dec","jan":"jan","feb":"feb","mar":"mar",
              "apr":"apr","jun":"jun","jul":"jul","aug":"aug","sep":"sep","oct":"oct",
              "nov":"nov","dec":"dec"}

    try:
        rows = get_rows()
    except Exception as e:
        await update.message.reply_text(f"Could not load sheet: {e}")
        return

    # Last N entries by category
    if "last" in tl:
        n = 10
        for word in tl.split():
            if word.isdigit(): n = int(word)
        for cat in CATEGORIES:
            if cat.lower() in tl:
                entries = search_entries(rows, category=cat, limit=n)
                await update.message.reply_text(format_entries(entries, f"Last {n} {cat} Entries"))
                return

    # Month search
    search_starters = ["show","find","search","last","get","list","share","entries for","entries in"]
    is_search = any(tl.startswith(s) for s in search_starters) or "entries" in tl
    if is_search:
        for month_name, month_code in months.items():
            if month_name in tl:
                entries = search_entries(rows, month=month_code)
                await update.message.reply_text(format_entries(entries, f"{month_name.capitalize()} Entries"))
                return

    # Keyword search
    for cmd in ["search","find","entries with","entries for"]:
        if cmd in tl:
            keyword = tl
            for c in ["search","find","entries with","entries for","entries","zakat","khair","asanee"]:
                keyword = keyword.replace(c,"").strip()
            if keyword and len(keyword) > 2:
                entries = search_entries(rows, keyword=keyword)
                await update.message.reply_text(format_entries(entries, f"Entries matching '{keyword}'"))
                return

    # New entry extraction
    await update.message.reply_text("Analyzing...")
    try:
        recent = "\n".join([f"{r[0]}|{r[1]}|{r[3]}|{r[4]}" for r in rows[-10:] if len(r)>=5])
    except: recent = ""
    try:
        entries = extract(text, recent=recent)
        if not entries or "error" in entries[0]:
            err = entries[0].get("error","unknown") if entries else "unknown"
            await update.message.reply_text(f"Could not extract: {err}\n\nTry again.")
            return
        ctx.user_data["pending"] = entries
        bal = get_balances()
        msg = f"I found {len(entries)} entr{'y' if len(entries)==1 else 'ies'}:\n\n"
        msg += format_pending(entries)
        msg += f"\nCurrent balances:\n{format_balances(bal)}\n\n"
        msg += "Reply YES to confirm all, or correct entries:\n"
        msg += "- 'entry 2 is Zakat'\n- 'entry 1 amount 500000'\n- 'entry 3 date Mar-26'"
        await update.message.reply_text(msg)
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")

async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USER_ID and update.effective_user.id != ALLOWED_USER_ID: return
    await update.message.reply_text("Reading screenshot...")
    try:
        file = await ctx.bot.get_file(update.message.photo[-1].file_id)
        with tempfile.NamedTemporaryFile(suffix=".jpg") as tmp:
            await file.download_to_drive(tmp.name)
            with open(tmp.name,"rb") as f: img_b64 = base64.b64encode(f.read()).decode()
        try:
            rows = get_rows()
            recent = "\n".join([f"{r[0]}|{r[1]}|{r[3]}|{r[4]}" for r in rows[-10:] if len(r)>=5])
        except: recent = ""
        entries = extract(update.message.caption or "", img_b64=img_b64, recent=recent)
        if not entries or "error" in entries[0]:
            await update.message.reply_text("Could not extract entries. Add a caption describing the payment.")
            return
        ctx.user_data["pending"] = entries
        bal = get_balances()
        msg = f"I found {len(entries)} entr{'y' if len(entries)==1 else 'ies'}:\n\n"
        msg += format_pending(entries)
        msg += f"\nCurrent balances:\n{format_balances(bal)}\n\n"
        msg += "Reply YES to confirm all, or correct entries:\n"
        msg += "- 'entry 2 is Zakat'\n- 'entry 1 amount 500000'\n- 'entry 3 date Mar-26'"
        await update.message.reply_text(msg)
    except Exception as e:
        await update.message.reply_text(f"Photo error: {e}")

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("balances", balances_cmd))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    logger.info("Bot started...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
