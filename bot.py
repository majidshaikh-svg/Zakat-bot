import os
import json
import logging
import tempfile
import anthropic
import gspread
import httpx
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes, CommandHandler
from google.oauth2.service_account import Credentials

# ── Logging ────────────────────────────────────────────────────────
logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
CLAUDE_API_KEY   = os.environ["CLAUDE_API_KEY"]
SHEET_ID         = os.environ["SHEET_ID"]
ALLOWED_USER_ID  = int(os.environ.get("ALLOWED_USER_ID", "0"))  # Your Telegram user ID
GOOGLE_CREDS     = os.environ["GOOGLE_CREDS"]  # JSON string of service account credentials

CATEGORIES = ["Zakat", "Khair", "Aasanee"]

# ── Google Sheets ──────────────────────────────────────────────────
def get_sheet():
    creds_dict = json.loads(GOOGLE_CREDS)
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    gc = gspread.authorize(creds)
    return gc.open_by_key(SHEET_ID).get_worksheet(0)

def get_balances():
    sheet = get_sheet()
    rows = sheet.get_all_values()
    balances = {cat: 0 for cat in CATEGORIES}
    for row in rows[1:]:  # skip header
        if len(row) >= 3:
            # Handle both column layouts
            if row[2] in CATEGORIES:
                category = row[2]
                try:
                    amount = float(str(row[1]).replace(",", ""))
                    balances[category] += amount
                except:
                    pass
            elif len(row) >= 4 and row[3] in CATEGORIES:
                category = row[3]
                try:
                    amount = float(str(row[1]).replace(",", ""))
                    balances[category] += amount
                except:
                    pass
    return balances

def append_entry(date, amount, category, details):
    sheet = get_sheet()
    sheet.append_row([date, amount, category, details])

# ── Claude API ─────────────────────────────────────────────────────
client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)

def extract_entry(text: str, audio_b64: str = None, image_b64: str = None, recent_entries: str = "") -> dict:
    content = []
    
    if image_b64:
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": image_b64}
        })
    if audio_b64:
        content.append({
            "type": "document", 
            "source": {"type": "base64", "media_type": "audio/ogg", "data": audio_b64}
        })
    
    content.append({"type": "text", "text": text or "See attached media."})
    
    system = f"""You extract charity payment entries for Majid's tracker.
Categories: Zakat, Khair, Aasanee.

Return ONLY raw JSON (no markdown, no backticks):
{{"date":"Apr-26","amount":50000,"category":"Zakat","details":"Mama Raja"}}

If unclear: {{"error":"reason"}}

Rules:
- Amount is always in PKR
- Date format: Mon-YY (e.g. Apr-26)
- If no date mentioned, use current month/year
- Details should be concise description
- Understand Urdu/English mix

Recent entries for duplicate check:
{recent_entries}"""

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=500,
        system=system,
        messages=[{"role": "user", "content": content}]
    )
    
    raw = response.content[0].text.strip()
    raw = raw.replace("```json", "").replace("```", "").strip()
    return json.loads(raw)

def transcribe_audio(audio_b64: str) -> str:
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=500,
        system="Transcribe this voice message exactly as spoken. Return only the transcription, nothing else.",
        messages=[{
            "role": "user",
            "content": [
                {"type": "document", "source": {"type": "base64", "media_type": "audio/ogg", "data": audio_b64}},
                {"type": "text", "text": "Transcribe this audio message."}
            ]
        }]
    )
    return response.content[0].text.strip()

# ── Helpers ────────────────────────────────────────────────────────
def fmt(n: float) -> str:
    if n >= 10_000_000:
        return f"{n/10_000_000:.2f}Cr"
    if n >= 100_000:
        return f"{n/100_000:.2f}L"
    return f"{n:,.0f}"

def format_balances(balances: dict) -> str:
    lines = []
    for cat in CATEGORIES:
        amt = balances.get(cat, 0)
        lines.append(f"  {cat}: {fmt(amt)} PKR")
    return "\n".join(lines)

def format_confirmation_card(entry: dict, balances: dict) -> str:
    return f"""📋 *I understood this as:*

📅 Date: `{entry['date']}`
🏷 Category: `{entry['category']}`
💰 Amount: `{int(entry['amount']):,} PKR`
📝 Details: `{entry['details']}`

*Current {entry['category']} balance:* `{fmt(balances[entry['category']])} PKR`

Reply *YES* to confirm or tell me what to correct."""

def format_saved_message(entry: dict, old_balances: dict, new_balances: dict) -> str:
    cat = entry['category']
    return f"""✅ *Saved to Google Sheets!*

{cat}: `{fmt(old_balances[cat])}` → *{fmt(new_balances[cat])} PKR*

*All balances:*
{format_balances(new_balances)}"""

# ── Bot Handlers ───────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    balances = get_balances()
    await update.message.reply_text(
        f"🕌 *Majid's Charity Tracker*\n\n"
        f"Send me any text, voice note, or screenshot and I'll update your sheets.\n\n"
        f"*Current balances:*\n{format_balances(balances)}",
        parse_mode="Markdown"
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Security check
    if ALLOWED_USER_ID and update.effective_user.id != ALLOWED_USER_ID:
        await update.message.reply_text("❌ Unauthorized.")
        return

    user_id = update.effective_user.id
    text = update.message.text or ""

    # Check if confirming a pending entry
    if text.upper().strip() in ["YES", "Y", "CONFIRM", "OK", "ہاں", "HAN"]:
        pending = context.user_data.get("pending_entry")
        if pending:
            try:
                old_balances = get_balances()
                append_entry(pending["date"], pending["amount"], pending["category"], pending["details"])
                new_balances = get_balances()
                context.user_data["pending_entry"] = None
                await update.message.reply_text(
                    format_saved_message(pending, old_balances, new_balances),
                    parse_mode="Markdown"
                )
            except Exception as e:
                await update.message.reply_text(f"❌ Error saving: {str(e)}")
        else:
            await update.message.reply_text("No pending entry to confirm. Send me some data first.")
        return

    # Check if cancelling
    if text.upper().strip() in ["NO", "CANCEL", "نہیں"]:
        context.user_data["pending_entry"] = None
        await update.message.reply_text("❌ Cancelled. Send me new data whenever you're ready.")
        return

    # Get recent entries for duplicate check
    try:
        sheet = get_sheet()
        rows = sheet.get_all_values()
        recent = []
        for row in rows[-10:]:
            if len(row) >= 4:
                recent.append(f"{row[0]}|{row[1]}|{row[2]}|{row[3]}")
        recent_str = "\n".join(recent)
    except:
        recent_str = ""

    # Extract entry from text
    await update.message.reply_text("🔍 Analyzing...")
    
    try:
        entry = extract_entry(text, recent_entries=recent_str)
        
        if "error" in entry:
            await update.message.reply_text(f"❓ {entry['error']}\n\nPlease try again with more details.")
            return
        
        # Store pending entry
        context.user_data["pending_entry"] = entry
        
        # Get balances and show confirmation
        balances = get_balances()
        
        # Duplicate check
        dup_warning = ""
        for row in rows[1:]:
            if len(row) >= 4:
                try:
                    if (float(str(row[1]).replace(",","")) == entry["amount"] and 
                        row[2] == entry["category"] and 
                        row[3].lower() == entry["details"].lower()):
                        dup_warning = "\n⚠️ *Possible duplicate detected!*\n"
                        break
                except:
                    pass
        
        msg = format_confirmation_card(entry, balances)
        if dup_warning:
            msg += dup_warning
            
        await update.message.reply_text(msg, parse_mode="Markdown")
        
    except json.JSONDecodeError:
        await update.message.reply_text("❓ Couldn't extract a clear entry. Please try again.")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Security check
    if ALLOWED_USER_ID and update.effective_user.id != ALLOWED_USER_ID:
        return

    await update.message.reply_text("🎙 Transcribing your voice note...")
    
    try:
        # Download voice file
        voice = update.message.voice
        file = await context.bot.get_file(voice.file_id)
        
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            await file.download_to_drive(tmp.name)
            with open(tmp.name, "rb") as f:
                audio_data = f.read()
        
        import base64
        audio_b64 = base64.b64encode(audio_data).decode()
        
        # Transcribe
        transcript = transcribe_audio(audio_b64)
        await update.message.reply_text(f"🎙 *Heard:* _{transcript}_", parse_mode="Markdown")
        
        # Now extract from transcript
        try:
            sheet = get_sheet()
            rows = sheet.get_all_values()
            recent = []
            for row in rows[-10:]:
                if len(row) >= 4:
                    recent.append(f"{row[0]}|{row[1]}|{row[2]}|{row[3]}")
            recent_str = "\n".join(recent)
        except:
            recent_str = ""
        
        entry = extract_entry(transcript, recent_entries=recent_str)
        
        if "error" in entry:
            await update.message.reply_text(f"❓ {entry['error']}\n\nPlease try again.")
            return
        
        context.user_data["pending_entry"] = entry
        balances = get_balances()
        
        await update.message.reply_text(
            format_confirmation_card(entry, balances),
            parse_mode="Markdown"
        )
        
    except Exception as e:
        await update.message.reply_text(f"❌ Voice processing error: {str(e)}")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Security check
    if ALLOWED_USER_ID and update.effective_user.id != ALLOWED_USER_ID:
        return

    await update.message.reply_text("📸 Reading your screenshot...")
    
    try:
        photo = update.message.photo[-1]  # highest resolution
        file = await context.bot.get_file(photo.file_id)
        
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            await file.download_to_drive(tmp.name)
            with open(tmp.name, "rb") as f:
                image_data = f.read()
        
        import base64
        image_b64 = base64.b64encode(image_data).decode()
        
        caption = update.message.caption or ""
        
        try:
            sheet = get_sheet()
            rows = sheet.get_all_values()
            recent = []
            for row in rows[-10:]:
                if len(row) >= 4:
                    recent.append(f"{row[0]}|{row[1]}|{row[2]}|{row[3]}")
            recent_str = "\n".join(recent)
        except:
            recent_str = ""
        
        entry = extract_entry(caption, image_b64=image_b64, recent_entries=recent_str)
        
        if "error" in entry:
            await update.message.reply_text(f"❓ {entry['error']}\n\nPlease try again.")
            return
        
        context.user_data["pending_entry"] = entry
        balances = get_balances()
        
        await update.message.reply_text(
            format_confirmation_card(entry, balances),
            parse_mode="Markdown"
        )
        
    except Exception as e:
        await update.message.reply_text(f"❌ Photo processing error: {str(e)}")

async def balances_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USER_ID and update.effective_user.id != ALLOWED_USER_ID:
        return
    balances = get_balances()
    await update.message.reply_text(
        f"💰 *Current Balances:*\n\n{format_balances(balances)}",
        parse_mode="Markdown"
    )

# ── Main ───────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("balances", balances_command))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    logger.info("Bot started...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
