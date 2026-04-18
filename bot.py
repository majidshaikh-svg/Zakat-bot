import os, json, logging, tempfile, base64, urllib.request, time
import anthropic
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN  = os.environ["TELEGRAM_TOKEN"]
CLAUDE_API_KEY  = os.environ["CLAUDE_API_KEY"]
ALLOWED_USER_ID = int(os.environ.get("ALLOWED_USER_ID", "0"))
SCRIPT_URL      = "https://script.google.com/macros/s/AKfycbzzE1itHnJ87R_ffxE5ZcRYth0Ds0_OOj46XGGjvW0gAi7CiE47L4ruTehZrefNY7uD/exec"
CATEGORIES      = ["Zakat", "Khair", "Aasanee"]

def get_balances():
    url = SCRIPT_URL + "?t=" + str(int(time.time()))
    with urllib.request.urlopen(url, timeout=15) as r:
        rows = json.loads(r.read().decode())
    bal = {c: 0 for c in CATEGORIES}
    for row in rows[1:]:
        cat = row[2] if len(row) > 2 and row[2] in CATEGORIES else (row[3] if len(row) > 3 and row[3] in CATEGORIES else None)
        if cat:
            try: bal[cat] += float(str(row[1]).replace(",",""))
            except: pass
    return bal

def get_rows():
    url = SCRIPT_URL + "?t=" + str(int(time.time()))
    with urllib.request.urlopen(url, timeout=15) as r:
        return json.loads(r.read().decode())

def append_entry(date, amount, category, details):
    data = json.dumps([date, amount, category, details]).encode()
    req = urllib.request.Request(SCRIPT_URL, data=data, method="POST")
    req.add_header("Content-Type", "text/plain")
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())

client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)

def fmt(n):
    if n >= 10000000: return f"{n/10000000:.2f}Cr"
    if n >= 100000: return f"{n/100000:.2f}L"
    return f"{n:,.0f}"

def format_balances(bal):
    return "\n".join([f"  {c}: {fmt(bal.get(c,0))} PKR" for c in CATEGORIES])

def extract(text, img_b64=None, recent=""):
    content = []
    if img_b64:
        content.append({"type":"image","source":{"type":"base64","media_type":"image/jpeg","data":img_b64}})
    content.append({"type":"text","text": text or "See attached."})
    system = f"""Extract ALL charity payment entries. Categories: Zakat, Khair, Aasanee.
Return ONLY a JSON array:
[{{"date":"Apr-26","amount":50000,"category":"Zakat","details":"Mama Raja"}}]
If nothing found: [{{"error":"reason"}}]
Rules:
- Amount in PKR. 1m=1000000, 1 lakh=100000, 1k=1000
- Date format Mon-YY. If no date use Apr-26
- Fix spelling mistakes in category names
Recent entries:
{recent}"""
    r = client.messages.create(model="claude-sonnet-4-20250514", max_tokens=1000, system=system, messages=[{"role":"user","content":content}])
    raw = r.content[0].text.strip().replace("```json","").replace("```","").strip()
    result = json.loads(raw)
    if isinstance(result, dict): result = [result]
    return result

def format_pending(entries):
    msg = ""
    for i, e in enumerate(entries):
        msg += f"{i+1}. {e['date']} | {e['category']} | {fmt(e['amount'])} PKR | {e['details']}\n"
    return msg

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        bal = get_balances()
        await update.message.reply_text(f"Majid Charity Tracker\n\nBalances:\n{format_balances(bal)}\n\nSend text, voice or screenshot!")
    except Exception as e:
        await update.message.reply_text(f"Bot running! Sheet error: {e}")

async def balances_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USER_ID and update.effective_user.id != ALLOWED_USER_ID: return
    try:
        bal = get_balances()
        await update.message.reply_text(f"Balances:\n\n{format_balances(bal)}")
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
                    append_entry(entry["date"], entry["amount"], entry["category"], entry["details"])
                new = get_balances()
                ctx.user_data["pending"] = []
                await update.message.reply_text(f"Saved!\n\nNew balances:\n{format_balances(new)}")
            except Exception as e:
                await update.message.reply_text(f"Error saving: {e}")
        else:
            await update.message.reply_text("No pending entry.")
        return

    if tl in ["no","cancel"]:
        ctx.user_data["pending"] = []
        await update.message.reply_text("Cancelled.")
        return

    await update.message.reply_text("Analyzing...")
    try:
        rows = get_rows()
        recent = "\n".join([f"{r[0]}|{r[1]}|{r[2]}|{r[3]}" for r in rows[-10:] if len(r)>=4])
    except: recent = ""
    try:
        entries = extract(text, recent=recent)
        if not entries or "error" in entries[0]:
            err = entries[0].get("error","unknown") if entries else "unknown"
            await update.message.reply_text(f"Could not extract: {err}\n\nTry again.")
            return
        ctx.user_data["pending"] = entries
        bal = get_balances()
        msg = f"I found {len(entries)} entries:\n\n"
        msg += format_pending(entries)
        msg += f"\nCurrent balances:\n{format_balances(bal)}\n\nReply YES to confirm."
        await update.message.reply_text(msg)
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")

async def handle_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USER_ID and update.effective_user.id != ALLOWED_USER_ID: return
    await update.message.reply_text("Transcribing...")
    try:
        file = await ctx.bot.get_file(update.message.voice.file_id)
        with tempfile.NamedTemporaryFile(suffix=".ogg") as tmp:
            await file.download_to_drive(tmp.name)
            with open(tmp.name,"rb") as f: audio_b64 = base64.b64encode(f.read()).decode()
        r = client.messages.create(model="claude-sonnet-4-20250514", max_tokens=300,
            system="Transcribe exactly. Return only transcription.",
            messages=[{"role":"user","content":[{"type":"document","source":{"type":"base64","media_type":"audio/ogg","data":audio_b64}},{"type":"text","text":"Transcribe."}]}])
        transcript = r.content[0].text.strip()
        await update.message.reply_text(f"Heard: {transcript}")
        update.message.text = transcript
        await handle_text(update, ctx)
    except Exception as e:
        await update.message.reply_text(f"Voice error: {e}")

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
            recent = "\n".join([f"{r[0]}|{r[1]}|{r[2]}|{r[3]}" for r in rows[-10:] if len(r)>=4])
        except: recent = ""
        entries = extract(update.message.caption or "", img_b64=img_b64, recent=recent)
        if not entries or "error" in entries[0]:
            await update.message.reply_text("Could not extract. Add a caption.")
            return
        ctx.user_data["pending"] = entries
        bal = get_balances()
        msg = f"I found {len(entries)} entries:\n\n"
        msg += format_pending(entries)
        msg += f"\nCurrent balances:\n{format_balances(bal)}\n\nReply YES to confirm."
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
