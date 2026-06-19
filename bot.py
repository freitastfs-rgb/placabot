import os
import json
import re
import base64
import httpx
import asyncio
from datetime import datetime, timezone, timedelta
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes

TZ_BRASIL = timezone(timedelta(hours=-3))

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
ANTHROPIC_KEY  = os.environ.get("ANTHROPIC_KEY")
DB_FILE        = "/data/placas.json" if os.path.exists("/data") else "placas.json"

if not TELEGRAM_TOKEN or not ANTHROPIC_KEY:
    raise SystemExit("ERRO: defina as variáveis de ambiente TELEGRAM_TOKEN e ANTHROPIC_KEY")

LAST_RECORD_BY_CHAT = {}

def load_db():
    if os.path.exists(DB_FILE):
        with open(DB_FILE, "r") as f:
            return json.load(f)
    return {}

def save_db(db):
    with open(DB_FILE, "w") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)

async def ler_placa(image_bytes: bytes) -> str:
    b64 = base64.b64encode(image_bytes).decode()
    payload = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 200,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
                {"type": "text", "text": "Leia a placa do veículo nesta imagem. Responda SOMENTE com JSON puro sem markdown: {\"placa\":\"ABC1234\"} — sem hífens, sem espaços. Se não encontrar placa: {\"placa\":\"NAO_ENCONTRADA\"}"}
            ]
        }]
    }
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json=payload
        )
        r.raise_for_status()
        data = r.json()
        text = data["content"][0]["text"].strip()
        result = json.loads(text.replace("```json","").replace("```","").strip())
        return result.get("placa", "NAO_ENCONTRADA").upper().replace("-","").replace(" ","")

def formatar(p):
    p = p.upper().replace("-","").replace(" ","")
    if len(p) == 7:
        if re.match(r'^[A-Z]{3}[0-9][A-Z][0-9]{2}$', p):
            return p
        return f"{p[:3]}-{p[3:]}"
    return p

PLATE_REGEX = re.compile(r'\b([A-Z]{3})[\s-]?(\d{4}|\d[A-Z]\d{2})\b', re.IGNORECASE)

def extrair_placa_do_texto(texto: str):
    if not texto:
        return None, texto
    m = PLATE_REGEX.search(texto)
    if not m:
        return None, texto
    placa_raw = (m.group(1) + m.group(2)).upper()
    resto = (texto[:m.start()] + texto[m.end():]).strip(" ,-—:\n")
    return placa_raw, resto

def registrar_placa(db, placa_raw, local, user, agora):
    placa = formatar(placa_raw)
    registro = {"placa": placa, "local": local if local else "Local não informado", "hora": agora, "usuario": user}
    alerta = False
    historico = []
    if placa_raw in db:
        alerta = True
        historico = list(db[placa_raw])
        db[placa_raw].append(registro)
    else:
        db[placa_raw] = [registro]
    return placa, alerta, historico, registro

def esc(t):
    for c in ['_','*','[',']','(',')','-','.','+','!','#']:
        t = t.replace(c, f'\\{c}')
    return t

def montar_resposta(placa, local_txt, agora, user, alerta, historico, total):
    if alerta:
        resp = f"⚠️ *ALERTA\\! PLACA REPETIDA\\!*\n\n"
        resp += f"🚗 *Placa:* `{esc(placa)}`\n"
        resp += f"📍 *Agora:* {esc(local_txt)}\n"
        resp += f"🕐 *Hora:* {esc(agora)}\n"
        resp += f"👤 *Visto por:* {esc(user)}\n\n"
        resp += f"📋 *Histórico \\({total} ocorrências\\):*\n"
        for i, h in enumerate(historico, 1):
            resp += f"{i}\\. {esc(h['hora'])} — {esc(h['local'])} \\({esc(h['usuario'])}\\)\n"
    else:
        resp = f"✅ *Placa registrada\\!*\n\n"
        resp += f"🚗 *Placa:* `{esc(placa)}`\n"
        resp += f"📍 *Local:* {esc(local_txt)}\n"
        resp += f"🕐 *Hora:* {esc(agora)}\n"
        resp += f"👤 *Registrado por:* {esc(user)}\n\n"
        resp += f"_Primeira vez que essa placa aparece\\._"
    return resp

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    user = msg.from_user.first_name or "Alguém"
    chat_id = msg.chat_id
    agora = datetime.now(TZ_BRASIL).strftime("%d/%m/%Y %H:%M")

    if msg.text and not msg.photo:
        placa_raw, local = extrair_placa_do_texto(msg.text)
        if not placa_raw:
            await msg.reply_text("📸 Me manda uma *foto* da placa, ou escreva a placa direto \\(ex: `ABC1234 Rua das Flores`\\)\\.", parse_mode="MarkdownV2")
            return
        db = load_db()
        placa, alerta, historico, registro = registrar_placa(db, placa_raw, local, user, agora)
        save_db(db)
        LAST_RECORD_BY_CHAT[chat_id] = {"placa_raw": placa_raw, "registro": registro}
        total = len(db[placa_raw])
        await msg.reply_text(montar_resposta(placa, registro["local"], agora, user, alerta, historico, total), parse_mode="MarkdownV2")
        return

    if not msg.photo:
        await msg.reply_text("📸 Me manda uma *foto* da placa\\!", parse_mode="MarkdownV2")
        return

    local = msg.caption.strip() if msg.caption else ""
    await msg.reply_text("🔍 Analisando placa\\.\\.\\.", parse_mode="MarkdownV2")

    try:
        photo = msg.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        image_bytes = await file.download_as_bytearray()
        placa_raw = await ler_placa(bytes(image_bytes))
        if placa_raw == "NAO_ENCONTRADA" or not placa_raw:
            await msg.reply_text("❌ Não consegui identificar a placa\\. Tente foto mais nítida ou digite a placa\\.", parse_mode="MarkdownV2")
            return
        db = load_db()
        placa, alerta, historico, registro = registrar_placa(db, placa_raw, local, user, agora)
        save_db(db)
        LAST_RECORD_BY_CHAT[chat_id] = {"placa_raw": placa_raw, "registro": registro}
        total = len(db[placa_raw])
        await msg.reply_text(montar_resposta(placa, registro["local"], agora, user, alerta, historico, total), parse_mode="MarkdownV2")
    except Exception as e:
        print(f"ERRO: {e}")
        await msg.reply_text("❌ Erro ao processar\\. Tente novamente\\.", parse_mode="MarkdownV2")

async def handle_editar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    chat_id = msg.chat_id
    if not context.args:
        await msg.reply_text("Use: `/editar Rua das Flores, em frente ao mercado`\nAtualiza o local do último registro\\.", parse_mode="MarkdownV2")
        return
    last = LAST_RECORD_BY_CHAT.get(chat_id)
    if not last:
        await msg.reply_text("❌ Nenhum registro recente para editar\\.", parse_mode="MarkdownV2")
        return
    novo_local = " ".join(context.args).strip()
    placa_raw = last["placa_raw"]
    registro_antigo = last["registro"]
    db = load_db()
    if placa_raw not in db or registro_antigo not in db[placa_raw]:
        await msg.reply_text("❌ Registro não encontrado\\.", parse_mode="MarkdownV2")
        return
    idx = db[placa_raw].index(registro_antigo)
    db[placa_raw][idx]["local"] = novo_local
    save_db(db)
    LAST_RECORD_BY_CHAT[chat_id]["registro"] = db[placa_raw][idx]
    await msg.reply_text(f"✅ *Local atualizado\\!*\n\n🚗 `{esc(db[placa_raw][idx]['placa'])}`\n📍 {esc(novo_local)}", parse_mode="MarkdownV2")

async def handle_lista(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = load_db()
    if not db:
        await update.message.reply_text("📋 Nenhuma placa registrada ainda\\.", parse_mode="MarkdownV2")
        return
    resp = f"📋 *Placas registradas: {len(db)}*\n\n"
    for placa_key, ocorrencias in sorted(db.items()):
        placa = ocorrencias[-1]["placa"]
        total = len(ocorrencias)
        ultimo = ocorrencias[-1]
        alerta = "⚠️ " if total > 1 else "🚗 "
        resp += f"{alerta}`{esc(placa)}` — {total}x — último: {esc(ultimo['local'])}\n"
    await update.message.reply_text(resp, parse_mode="MarkdownV2")

async def handle_placa(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Use: /placa ABC1234", parse_mode="MarkdownV2")
        return
    placa_raw = context.args[0].upper().replace("-","").replace(" ","")
    db = load_db()
    if placa_raw not in db:
        await update.message.reply_text(f"❌ Placa `{esc(placa_raw)}` não encontrada\\.", parse_mode="MarkdownV2")
        return
    ocorrencias = db[placa_raw]
    placa = ocorrencias[0]["placa"]
    resp = f"🔎 *Histórico da placa `{esc(placa)}`*\n\n"
    for i, h in enumerate(ocorrencias, 1):
        resp += f"*{i}\\.* {esc(h['hora'])}\n   📍 {esc(h['local'])}\n   👤 {esc(h['usuario'])}\n\n"
    await update.message.reply_text(resp, parse_mode="MarkdownV2")

def main():
    from telegram.ext import CommandHandler
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.PHOTO | filters.TEXT, handle_message))
    app.add_handler(CommandHandler("lista", handle_lista))
    app.add_handler(CommandHandler("placa", handle_placa))
    app.add_handler(CommandHandler("editar", handle_editar))
    print("🤖 PlacaMonitor Bot rodando...")
    app.run_polling()

if __name__ == "__main__":
    main()
