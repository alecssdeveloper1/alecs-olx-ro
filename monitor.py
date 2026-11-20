import os
import re
import json
import html
import sqlite3
import sys
import time
import threading
from datetime import datetime
import requests
from bs4 import BeautifulSoup
from google import genai
import benchmarks
import market_analytics
import olx_ro

from dotenv import load_dotenv

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="backslashreplace")

# Carrega configurações e segredos do arquivo .env
load_dotenv()

# ================= CONFIGURAÇÕES =================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DB_PATH = os.getenv("DB_PATH", os.path.join(os.path.dirname(__file__), "ofertas_ro.db"))
INTERVALO_SEGUNDOS = int(os.getenv("INTERVALO_SEGUNDOS", "90"))
MAX_PAGES_PER_SEARCH = 5
MAX_RESULTS_PER_PAGE = 15
SEARCH_PAGE_DELAY_SECONDS = 2
OLX_USER_AGENT = os.getenv(
    "OLX_USER_AGENT", "GarimpoOLXBot/1.0 (personal use)"
)
OLX_ACCESS_BLOCKED = False
# Stare conversație pe chat: după butonul Adaugă, așteaptă modelul scris de utilizator.
pending_actions = {}

MAIN_KEYBOARD = {
    "keyboard": [
        [{"text": "➕ Adaugă"}, {"text": "📋 Listă"}, {"text": "🗑 Șterge"}],
        [{"text": "🏆 Top"}, {"text": "📊 Tierlist"}, {"text": "❓ Ajutor"}],
    ],
    "resize_keyboard": True,
    "persistent": True,
}

BUTTON_ALIASES = {
    "➕ adaugă": "add",
    "➕ adauga": "add",
    "adaugă": "add",
    "adauga": "add",
    "📋 listă": "list",
    "📋 lista": "list",
    "listă": "list",
    "lista": "list",
    "🗑 șterge": "del",
    "🗑 sterge": "del",
    "șterge": "del",
    "sterge": "del",
    "🏆 top": "top",
    "top": "top",
    "📊 tierlist": "tierlist",
    "tierlist": "tierlist",
    "❓ ajutor": "help",
    "ajutor": "help",
}

if not TELEGRAM_TOKEN or not GEMINI_API_KEY:
    raise ValueError("TELEGRAM_TOKEN și GEMINI_API_KEY trebuie configurate în fișierul .env")

ai_client = genai.Client(api_key=GEMINI_API_KEY)


class OLXAccessDeniedError(RuntimeError):
    pass


def fetch_olx_page(url, timeout):
    response = requests.get(
        url,
        headers={"User-Agent": OLX_USER_AGENT},
        timeout=timeout,
    )
    if response.status_code in {403, 429}:
        raise OLXAccessDeniedError(
            f"OLX.ro returned HTTP {response.status_code}; stopping this scan."
        )
    response.raise_for_status()
    return response


# ================= BANCO DE DADOS =================
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()
    # Tabela de anúncios
    c.execute("""
        CREATE TABLE IF NOT EXISTS ads (
            id TEXT PRIMARY KEY,
            title TEXT,
            price REAL,
            url TEXT,
            score INTEGER,
            review TEXT,
            tier TEXT,
            benchmark_review TEXT,
            market_review TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Migrações seguras de colunas
    for col in ["tier TEXT", "benchmark_review TEXT", "market_review TEXT"]:
        try:
            c.execute(f"ALTER TABLE ads ADD COLUMN {col}")
        except Exception:
            pass
            
    # Tabela de buscas dinâmicas por usuário
    c.execute("""
        CREATE TABLE IF NOT EXISTS searches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT,
            query TEXT,
            max_price REAL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Tabela de notificações já enviadas para evitar duplicados por usuário
    c.execute("""
        CREATE TABLE IF NOT EXISTS sent_notifications (
            ad_id TEXT,
            chat_id TEXT,
            PRIMARY KEY (ad_id, chat_id)
        )
    """)
    # Tabela de estado para contar postagens e disparar tiers periódicos
    c.execute("""
        CREATE TABLE IF NOT EXISTS bot_state (
            key TEXT PRIMARY KEY,
            value INTEGER
        )
    """)
    c.execute("INSERT OR IGNORE INTO bot_state (key, value) VALUES ('ads_since_last_digest', 0)")
    conn.commit()
    conn.close()

def ad_ja_notificado(ad_id, chat_id):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT 1 FROM sent_notifications WHERE ad_id = ? AND chat_id = ?", (ad_id, str(chat_id)))
    res = c.fetchone()
    conn.close()
    return res is not None

def registrar_notificacao(ad_id, chat_id):
    conn = get_db()
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO sent_notifications (ad_id, chat_id) VALUES (?, ?)", (ad_id, str(chat_id)))
    conn.commit()
    conn.close()

def salvar_ad_cache(ad_id, title, price, url, score=None, review=None, tier=None, pitaco_benchmark=None, pitaco_mercado=None):
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        INSERT OR REPLACE INTO ads (id, title, price, url, score, review, tier, benchmark_review, market_review)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (ad_id, title, price, url, score, review, tier, pitaco_benchmark, pitaco_mercado))
    conn.commit()
    conn.close()

def obter_ad_cache(ad_id):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM ads WHERE id = ?", (ad_id,))
    res = c.fetchone()
    conn.close()
    return res


# ================= AVALIAÇÃO COM IA MULTI-AGENTE =================
def avaliar_com_ia(title, description, price, query, max_price=None):
    hw_nome, hw_tipo, hw_info = benchmarks.identifica_hardware(title)
    stats_mercado = market_analytics.obtine_statistici_piata(
        query, price, pret_maxim=max_price
    )
    sample_count = stats_mercado["total_esantioane"]
    market_reference = (
        "mediană observată"
        if sample_count >= 3
        else "estimare orientativă (date insuficiente)"
    )
    market_position = olx_ro.describe_market_position(stats_mercado["discount_pct"])
    
    texto_benchmark = ""
    if hw_tipo == "telefon" and hw_info:
        texto_benchmark = (
            f"- Segment: {hw_info['segment']}\n"
            f"- Verificări flip: {hw_info['puncte_atentie']}\n"
            f"- Riscuri tipice: {hw_info['riscuri']}\n"
            f"- Sfat revânzare: {hw_info['sfat_revanzare']}"
        )
    elif hw_tipo == "gpu" and hw_info:
        custo_pt = benchmarks.calculeaza_cost_pe_punct(price, hw_info.get("scor", 0))
        texto_benchmark = (
            f"- Scor PassMark G3D: {hw_info['scor']} puncte\n"
            f"- Consum / conector: {hw_info['tdp']} W ({hw_info['conector_alimentare']})\n"
            f"- Memorie: {hw_info['vram']}\n"
            f"- Cost per punct G3D: {olx_ro.format_price(custo_pt)}"
        )
    elif hw_tipo == "console" and hw_info:
        texto_benchmark = (
            f"- Ecran: {hw_info['ecran']}\n"
            f"- Ecosistem și deblocare: {hw_info['ecosistem']}\n"
            f"- Capacități de emulare: {hw_info['emulare']}\n"
            f"- Verificări importante: {hw_info['puncte_atentie']}"
        )
    else:
        texto_benchmark = "- Nu există date tehnice preînregistrate; nu inventa specificații."

    prompt = f"""Ești evaluator de oferte second-hand pe OLX România, specializat pe TELEFOANE pentru arbitraj (cumpăr ieftin → revând cu profit).
Scrie toate textele exclusiv în limba română.
Bazează concluzia pe datele numerice furnizate și nu le contrazice.

ANUNȚ EVALUAT:
- Căutare: "{query}"
- Titlu: "{title}"
- Preț: {olx_ro.format_price(price)}
- Descriere: {description}

PROFIL PRODUS:
- Identificat: {hw_nome or 'necunoscut'} ({hw_tipo or 'general'})
{texto_benchmark}

DATE DE PIAȚĂ:
- Referință de preț ({market_reference}): {olx_ro.format_price(stats_mercado['mediana'])}
- Poziția prețului: {market_position}
- Anunțuri din istoric folosite: {sample_count}
- Lichiditate orientativă: {stats_mercado['lichiditate']}; timpul estimat: {stats_mercado['timp_vanzare']}
- Lichiditatea și timpul sunt euristici; nu le prezenta ca garanții.
- Revânzare rapidă estimată: {olx_ro.format_price(stats_mercado['pret_revanzare_rapida'])}; marjă brută estimată: {olx_ro.format_price(stats_mercado['marja_profit'])}

RĂSPUNDE STRICT CU UN OBIECT JSON:
{{
  "aprovado": true sau false,
  "nota": număr întreg între 0 și 10,
  "tier": "Tier S" | "Tier A" | "Tier B" | "Tier C",
  "pitaco_benchmark": "1–2 propoziții despre stare/riscuri telefon (baterie, lock, ecran, variantă)",
  "pitaco_mercado": "1–2 propoziții despre preț vs mediană și potențial de revânzare",
  "parecer_resumo": "o concluzie scurtă: merită sau nu pentru flip"
}}

Reguli:
- Potrivire strictă pe model: „Honor 600” ≠ Smart/Pro/Lite; „iPhone 12” ≠ 12 Pro/Pro Max.
- Respinge accesorii (husă, folie, încărcător), piese, telefoane defecte, iCloud/Google lock, preț 1 leu, schimburi fără preț clar.
- Nu inventa baterie %, stocare sau garanții dacă nu apar în anunț.
- Dacă prețul e peste mediană, spune explicit; nu îl numi chilipir.
- Nu acorda Tier S/A sub 20% discount. Sub 3 anunțuri în istoric → maxim Tier B.
- Tier S ≥35% sub mediană; Tier A 20–34,9%; Tier B 0–19,9%; Tier C peste mediană / riscant.
- Scor: S 9–10, A 7–8, B 5–6, C 0–4.
- Aprobă doar dacă există marjă realistă de revânzare pe modelul exact.
"""
    last_error = None
    for model_name in ["gemini-3.1-flash-lite", "gemini-3.5-flash-lite", "gemini-3.5-flash"]:
        try:
            res = ai_client.models.generate_content(
                model=model_name,
                contents=prompt
            )
            text = res.text.strip()
            text = re.sub(r"^```json\s*", "", text)
            text = re.sub(r"^```\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
            data = json.loads(text)
            aprovado = data.get("aprovado") is True
            nota, tier = olx_ro.align_score_with_market(
                data.get("nota", 5),
                stats_mercado["discount_pct"],
                sample_count,
            )
            if not aprovado:
                nota, tier = 0, "Tier C"
            return {
                "aprovado": aprovado,
                "nota": nota,
                "tier": tier,
                "parecer": str(data.get("parecer_resumo", "Evaluarea nu conține o concluzie.")),
                "pitaco_benchmark": str(data.get("pitaco_benchmark", "")),
                "pitaco_mercado": str(data.get("pitaco_mercado", "")),
                "stats_mercado": stats_mercado
            }
        except Exception as error:
            last_error = error
            continue
            
    raise RuntimeError("Gemini nu a returnat o evaluare validă.") from last_error

def interpretar_intencao_usuario(texto):
    """Folosește Gemini pentru a înțelege cererile în limbaj natural și a extrage termenul și prețul."""
    prompt = f"""Utilizatorul a trimis un mesaj unui bot de monitorizare OLX România:
"{texto}"

Identifică acțiunea dorită și răspunde STRICT în format JSON, în limba română:
{{
  "acao": "adicionar" | "listar" | "remover" | "ajuda" | "desconhecido",
  "termo": "numele produsului căutat, doar pentru acțiunea adicionar",
  "preco_maximo": "preț maxim numeric; null dacă utilizatorul nu menționează unul",
  "resposta": "mesaj scurt și prietenos în română"
}}

Exemple:
- "Caut un Nintendo Switch până la 1.200 lei" -> {{"acao": "adicionar", "termo": "Nintendo Switch", "preco_maximo": 1200.0, "resposta": "Voi căuta un Nintendo Switch până la 1.200 lei."}}
- "Anunță-mă dacă găsești un controller PS5 sub 250 lei" -> {{"acao": "adicionar", "termo": "Controller PS5", "preco_maximo": 250.0, "resposta": "Voi căuta un controller PS5 până la 250 lei."}}
- "care sunt căutările mele?" -> {{"acao": "listar", "termo": null, "preco_maximo": null, "resposta": ""}}
"""
    for model_name in ["gemini-3.1-flash-lite", "gemini-3.5-flash-lite"]:
        try:
            res = ai_client.models.generate_content(
                model=model_name,
                contents=prompt
            )
            text = res.text.strip()
            text = re.sub(r"^```json\s*", "", text)
            text = re.sub(r"^```\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
            return json.loads(text)
        except Exception:
            continue
    return {"acao": "desconhecido", "resposta": "Nu am înțeles. Spune ce cauți și prețul maxim, de exemplu: «Caut SSD 480GB până la 150 lei»."}


# ================= TELEGRAM =================
def send_telegram(chat_id, title, price, url, description, score, review, pitaco_benchmark=None, pitaco_mercado=None, stats_mercado=None, image_url=None):
    if score >= 9:
        badge = f"🔥 <b>SCOR IA: {score}/10 — OFERTĂ EXCELENTĂ (TIER S)</b> 🔥"
    elif score >= 7:
        badge = f"⭐ <b>SCOR IA: {score}/10 — OFERTĂ BUNĂ (TIER A)</b>"
    elif score >= 5:
        badge = f"📊 <b>SCOR IA: {score}/10 (TIER B)</b>"
    else:
        badge = f"📊 <b>SCOR IA: {score}/10 (TIER C)</b>"

    mercado_linha = ""
    if stats_mercado and stats_mercado.get("mediana"):
        sample_count = stats_mercado.get("total_esantioane", 0)
        reference_label = "Mediană observată" if sample_count >= 3 else "Estimare orientativă"
        sample_label = f"{sample_count} anunțuri" if sample_count != 1 else "1 anunț"
        market_position = olx_ro.describe_market_position(
            stats_mercado.get("discount_pct", 0)
        )
        mercado_linha = (
            f"📊 <b>Comparație cu piața:</b>\n"
            f"• {reference_label}: {olx_ro.format_price(stats_mercado['mediana'])} ({sample_label})\n"
            f"• Prețul este {market_position}.\n"
        )

    pareceres = ""
    if pitaco_benchmark:
        pareceres += f"\n⚙️ <b>Analiză tehnică:</b>\n<i>{html.escape(pitaco_benchmark)}</i>\n"
    if pitaco_mercado:
        pareceres += f"\n📈 <b>Analiză de piață:</b>\n<i>{html.escape(pitaco_mercado)}</i>\n"

    short_description = description.strip()
    if len(short_description) > 180:
        short_description = f"{short_description[:180].rstrip()}…"
    caption = (
        f"{badge}\n\n"
        f"📦 <b>Produs:</b> {html.escape(title)}\n"
        f"💰 <b>Preț cerut:</b> {olx_ro.format_price(price)}\n"
        f"{mercado_linha}"
        f"🧠 <b>Concluzie:</b> <i>{html.escape(review)}</i>\n"
        f"{pareceres}\n"
        f"🔗 <a href=\"{html.escape(url, quote=True)}\">Deschide anunțul pe OLX</a>\n\n"
        f"📝 <b>Descriere:</b>\n{html.escape(short_description)}"
    )
    
    try:
        if image_url:
            r = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto",
                json={
                    "chat_id": chat_id,
                    "photo": image_url,
                    "caption": caption,
                    "parse_mode": "HTML"
                },
                timeout=15
            )
            if r.status_code == 200:
                return
        
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": caption,
                "parse_mode": "HTML",
                "disable_web_page_preview": False
            },
            timeout=15
        )
    except Exception as e:
        print(f"Eroare la trimiterea către Telegram: {e}")

def send_msg(chat_id, text, reply_markup=None):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json=payload,
            timeout=10,
        )
    except Exception as e:
        print(f"Eroare la trimiterea mesajului: {e}")


def answer_callback(callback_query_id, text=None):
    payload = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery",
            json=payload,
            timeout=10,
        )
    except Exception as e:
        print(f"Eroare la answerCallbackQuery: {e}")


def parse_add_input(texto):
    """Extrage modelul și prețul maxim din text liber, ex: «honor 600 1700 lei»."""
    partes = texto.strip().split()
    if not partes:
        return None, None

    price_token_count = 2 if partes[-1].lower() in {"lei", "ron"} else 1
    if len(partes) > price_token_count:
        try:
            preco_val = olx_ro.parse_price(" ".join(partes[-price_token_count:]))
            termo_val = " ".join(partes[:-price_token_count]).strip()
            if termo_val:
                return termo_val, preco_val
        except ValueError:
            pass

    return " ".join(partes).strip(), 999999.0


def add_search(chat_id, termo, preco):
    conn = get_db()
    c = conn.cursor()
    c.execute(
        "INSERT INTO searches (chat_id, query, max_price) VALUES (?, ?, ?)",
        (chat_id, termo, preco),
    )
    conn.commit()
    conn.close()
    preco_msg = olx_ro.format_price(preco) if preco < 999999 else "Fără limită de preț"
    send_msg(
        chat_id,
        f"🎯 *Căutare adăugată!*\n\n📦 *Produs:* `{termo}`\n💰 *Preț maxim:* {preco_msg}\n\nTe anunț când găsesc un anunț potrivit pe modelul exact.",
        reply_markup=MAIN_KEYBOARD,
    )


def list_searches(chat_id):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT id, query, max_price FROM searches WHERE chat_id = ?", (chat_id,))
    rows = c.fetchall()
    conn.close()

    if not rows:
        send_msg(
            chat_id,
            "Nu ai căutări active.\nApasă *➕ Adaugă*, apoi scrie modelul, de exemplu: `honor 600 1700`",
            reply_markup=MAIN_KEYBOARD,
        )
        return []

    resp = "📋 *CĂUTĂRILE TALE ACTIVE:*\n\n"
    for i, row in enumerate(rows, 1):
        preco_str = olx_ro.format_price(row["max_price"]) if row["max_price"] else "Fără limită"
        resp += f"*{i}.* `{row['query']}` (până la {preco_str}) — id `{row['id']}`\n"
    send_msg(chat_id, resp, reply_markup=MAIN_KEYBOARD)
    return rows


def prompt_delete_searches(chat_id):
    rows = list_searches(chat_id)
    if not rows:
        return

    inline_keyboard = []
    for row in rows:
        preco_str = (
            olx_ro.format_price(row["max_price"])
            if row["max_price"] and row["max_price"] < 999999
            else "fără limită"
        )
        label = f"❌ {row['query'][:28]} ({preco_str})"
        inline_keyboard.append(
            [{"text": label, "callback_data": f"del:{row['id']}"}]
        )
    send_msg(
        chat_id,
        "Alege căutarea de șters (buton) sau scrie ID-ul, ex: `3`.\nTrimite `anulează` ca să renunți.",
        reply_markup={"inline_keyboard": inline_keyboard},
    )
    pending_actions[chat_id] = {"action": "del"}


def send_help(chat_id):
    help_msg = (
        "👋 *Garimpo OLX — telefoane / arbitraj*\n\n"
        "Găsesc telefoane sub prețul pieței pe *model exact*, ca să le revinzi cu profit.\n\n"
        "Butoane:\n"
        "• *➕ Adaugă* → scrie modelul, ex: `honor 600 1500`\n"
        "• *📋 Listă* / *🗑 Șterge*\n"
        "• *🏆 Top* / *📊 Tierlist*\n\n"
        "Exemple: `iphone 12 1200`, `samsung s23 1500`, `honor 600 pro 2500`\n"
        "Filtru: `iphone 12` ≠ 12 Pro; `honor 600` ≠ Smart/Pro."
    )
    send_msg(chat_id, help_msg, reply_markup=MAIN_KEYBOARD)


def show_tierlist(chat_id):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT query, max_price FROM searches WHERE chat_id = ?", (chat_id,))
    buscas_usuario = c.fetchall()

    if not buscas_usuario:
        conn.close()
        send_msg(
            chat_id,
            "Nu ai căutări active. Apasă *➕ Adaugă* și scrie modelul.",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    resp = "📊 *LISTA TA DE OFERTE*\n\n"
    for b in buscas_usuario:
        termo = b["query"]
        preco_limite = b["max_price"] or 999999.0
        palavras = [p for p in termo.split() if len(p) > 2]
        where_clauses = " AND ".join(["LOWER(title) LIKE ?" for _ in palavras]) or "1=1"
        params = [f"%{p.lower()}%" for p in palavras] + [preco_limite]
        query_sql = f"""
            SELECT title, price, score, url
            FROM ads
            WHERE {where_clauses}
              AND price <= ?
              AND score >= 6
            ORDER BY score DESC, created_at DESC
            LIMIT 3
        """
        c.execute(query_sql, params)
        ofertas_termo = [
            o
            for o in c.fetchall()
            if olx_ro.title_matches_exact_model(termo, o["title"])
        ]

        resp += f"🏷️ *{termo.upper()}:*\n"
        if ofertas_termo:
            for i, o in enumerate(ofertas_termo, 1):
                resp += (
                    f"  *{i}.* [{o['title'][:38]}]({o['url']}) - "
                    f"*{olx_ro.format_price(o['price'])}* (Tier {o['score']}/10)\n"
                )
        else:
            resp += "  _Nu am găsit încă oferte cu scor mare pentru acest model._\n"
        resp += "\n"

    conn.close()
    resp += "_Doar modelul exact din căutările tale._"
    send_msg(chat_id, resp, reply_markup=MAIN_KEYBOARD)


def show_top(chat_id):
    conn = get_db()
    c = conn.cursor()
    c.execute(
        """
        SELECT title, price, score, review, url
        FROM ads
        WHERE score >= 8
        ORDER BY score DESC, created_at DESC
        LIMIT 8
        """
    )
    rows = c.fetchall()
    conn.close()

    if not rows:
        send_msg(
            chat_id,
            "Nu am găsit încă oferte cu scor mare (8–10).",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    resp = "🏆 *CELE MAI BUNE OFERTE (8–10/10):*\n\n"
    for r in rows:
        badge = "🔥 TIER 10/10" if r["score"] == 10 else f"⭐ TIER {r['score']}/10"
        resp += (
            f"*{badge}*\n"
            f"📦 *{r['title']}*\n"
            f"💰 {olx_ro.format_price(r['price'])}\n"
            f"🧠 _{r['review']}_\n"
            f"🔗 [Vezi anunțul OLX]({r['url']})\n\n"
        )
    send_msg(chat_id, resp, reply_markup=MAIN_KEYBOARD)


def delete_search(chat_id, del_id):
    conn = get_db()
    c = conn.cursor()
    c.execute(
        "DELETE FROM searches WHERE id = ? AND chat_id = ?",
        (del_id, chat_id),
    )
    deleted = conn.total_changes > 0
    conn.commit()
    conn.close()
    if deleted:
        send_msg(chat_id, f"✅ Căutarea #{del_id} a fost ștearsă.", reply_markup=MAIN_KEYBOARD)
    else:
        send_msg(chat_id, f"❌ Nu am găsit căutarea #{del_id}.", reply_markup=MAIN_KEYBOARD)
    return deleted


# ================= SCRAPING =================
def extrair_detalhes_ad(url):
    try:
        r = fetch_olx_page(url, timeout=12)
        if r.status_code != 200:
            return None
        
        soup = BeautifulSoup(r.text, "html.parser")
        for s in soup.find_all("script", type="application/ld+json"):
            if not s.string:
                continue
            try:
                data = json.loads(s.string)
                if data.get("@type") == "Product" or "offers" in data:
                    name = data.get("name", "")
                    # A OLX manda a descrição com HTML (<br>, &amp;); a IA e o Telegram recebem texto limpo
                    description = BeautifulSoup(data.get("description", ""), "html.parser").get_text(" ", strip=True)
                    offers = data.get("offers", {})
                    if isinstance(offers, list):
                        offers = offers[0] if offers else {}
                    currency = str(offers.get("priceCurrency", "RON")).upper()
                    if currency not in {"RON", "LEI"}:
                        continue
                    price = olx_ro.parse_price(offers.get("price", 0))
                    
                    images = data.get("image", [])
                    image_url = None
                    if isinstance(images, list) and images:
                        first = images[0]
                        if isinstance(first, dict):
                            image_url = first.get("contentUrl")
                        elif isinstance(first, str):
                            image_url = first
                    elif isinstance(images, str):
                        image_url = images
                    
                    return {
                        "title": name,
                        "description": description,
                        "price": price,
                        "image_url": image_url
                    }
            except Exception:
                continue
    except OLXAccessDeniedError:
        raise
    except Exception as e:
        print(f"Eroare la extragerea detaliilor pentru {url}: {e}")
    return None

def monitorar_rodada():
    global OLX_ACCESS_BLOCKED
    if OLX_ACCESS_BLOCKED:
        return

    print(f"[{datetime.now().strftime('%H:%M:%S')}] Încep scanarea căutărilor active...")
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT id, chat_id, query, max_price FROM searches")
    buscas = c.fetchall()
    conn.close()
    
    if not buscas:
        print("Nu există căutări active.")
        return

    for b in buscas:
        search_id = b["id"]
        chat_id = str(b["chat_id"])
        query = b["query"]
        teto_preco = float(b["max_price"] or 999999)
        
        try:
            links_ad = []
            seen_page_ids = set()
            for page in range(1, MAX_PAGES_PER_SEARCH + 1):
                url_busca = olx_ro.build_search_url(query, page)
                try:
                    r = fetch_olx_page(url_busca, timeout=15)
                except requests.HTTPError as error:
                    if error.response is not None and error.response.status_code == 404:
                        break
                    raise
                if r.status_code != 200:
                    break

                page_links = olx_ro.parse_listing_links(r.text)
                if not page_links:
                    break

                new_page_links = [
                    (ad_id, href)
                    for ad_id, href in page_links
                    if ad_id not in seen_page_ids
                ]
                if not new_page_links:
                    break

                seen_page_ids.update(ad_id for ad_id, _ in page_links)
                links_ad.extend(new_page_links[:MAX_RESULTS_PER_PAGE])
                if page < MAX_PAGES_PER_SEARCH:
                    time.sleep(SEARCH_PAGE_DELAY_SECONDS)
                        
            # Procesează cel mult 15 anunțuri din fiecare pagină.
            for ad_id, href in links_ad:
                # Omite anunțurile pe care utilizatorul le-a primit deja.
                if ad_ja_notificado(ad_id, chat_id):
                    continue
                
                # Verifică memoria cache pentru a evita reevaluarea anunțurilor procesate.
                cached = obter_ad_cache(ad_id)
                if cached and cached["score"] is not None:
                    titulo = cached["title"]
                    preco = cached["price"]
                    if not olx_ro.title_matches_exact_model(query, titulo):
                        registrar_notificacao(ad_id, chat_id)
                        continue
                    desc = "Anunț verificat în cache."
                    score = cached["score"]
                    parecer = cached["review"]
                    tier = cached["tier"] if "tier" in cached.keys() else "Tier B"
                    pitaco_benchmark = cached["benchmark_review"] if "benchmark_review" in cached.keys() else None
                    pitaco_mercado = cached["market_review"] if "market_review" in cached.keys() else None
                    stats_mercado = None
                    img = None
                else:
                    detalhes = extrair_detalhes_ad(href)
                    if not detalhes:
                        salvar_ad_cache(ad_id, "Necunoscut", 0, href)
                        continue
                    
                    titulo = detalhes["title"]
                    preco = detalhes["price"]
                    desc = detalhes["description"]
                    img = detalhes["image_url"]

                    if not olx_ro.title_matches_exact_model(query, titulo):
                        salvar_ad_cache(
                            ad_id,
                            titulo,
                            preco,
                            href,
                            score=0,
                            review="Model diferit de căutarea exactă",
                        )
                        registrar_notificacao(ad_id, chat_id)
                        continue

                    # La telefoane, sare peste accesorii/piese evidente.
                    if market_analytics.este_cautare_telefon(query):
                        titlu_l = titulo.lower()
                        if any(
                            t in titlu_l for t in market_analytics.TERMENI_IGNORA_TELEFON
                        ):
                            salvar_ad_cache(
                                ad_id,
                                titulo,
                                preco,
                                href,
                                score=0,
                                review="Accesoriu/piesă, nu telefon complet",
                            )
                            registrar_notificacao(ad_id, chat_id)
                            continue
                    
                    if preco > teto_preco or preco <= 5:
                        salvar_ad_cache(ad_id, titulo, preco, href, score=0, review="Preț în afara limitei")
                        registrar_notificacao(ad_id, chat_id)
                        continue
                    
                    print(f"🧠 Analiză AI pentru [{chat_id}]: '{titulo}' la {olx_ro.format_price(preco)}...")
                    avaliacao = avaliar_com_ia(titulo, desc, preco, query, teto_preco)
                    score = avaliacao["nota"]
                    parecer = avaliacao["parecer"]
                    aprovado = avaliacao["aprovado"]
                    tier = avaliacao.get("tier", "Tier B")
                    pitaco_benchmark = avaliacao.get("pitaco_benchmark")
                    pitaco_mercado = avaliacao.get("pitaco_mercado")
                    stats_mercado = avaliacao.get("stats_mercado")
                    
                    if not aprovado:
                        score = 0
                        
                    salvar_ad_cache(ad_id, titulo, preco, href, score, parecer, tier, pitaco_benchmark, pitaco_mercado)
                
                # Notifică utilizatorul pentru scoruri de cel puțin 6, în limita de preț.
                if score >= 6 and preco <= teto_preco:
                    print(f"🎯 Trimit notificarea către {chat_id}: {titulo} [{score}/10 - {tier}]")
                    send_telegram(chat_id, titulo, preco, href, desc, score, parecer, pitaco_benchmark, pitaco_mercado, stats_mercado, img)
                    
                    # Actualizează contorul pentru a trimite un rezumat la fiecare 15 oferte.
                    verificar_e_enviar_digest_periodico(chat_id)
                    
                registrar_notificacao(ad_id, chat_id)
                time.sleep(1.5)
                
        except OLXAccessDeniedError as e:
            print(e)
            OLX_ACCESS_BLOCKED = True
            return
        except Exception as e:
            print(f"Eroare la căutarea „{query}”: {e}")
            
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Scanarea s-a încheiat.")

def verificar_e_enviar_digest_periodico(chat_id):
    """Generează un rezumat cu cele mai bune oferte la fiecare 15 notificări trimise."""
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT value FROM bot_state WHERE key = 'ads_since_last_digest'")
    row = c.fetchone()
    count = (row["value"] if row else 0) + 1
    
    if count >= 15:
        c.execute("UPDATE bot_state SET value = 0 WHERE key = 'ads_since_last_digest'")
        conn.commit()
        
        # Selectează cele mai bune oferte recente (scoruri de la 7 la 10).
        c.execute("""
            SELECT title, price, score, url 
            FROM ads 
            WHERE score >= 7 
            ORDER BY score DESC, created_at DESC 
            LIMIT 15
        """)
        top_ads = c.fetchall()
        conn.close()
        
        if top_ads:
            # Agrupa por categoria
            ram_list = []
            ssd_list = []
            gpu_list = []
            games_list = []
            outros_list = []
            
            for ad in top_ads:
                t = ad["title"].lower()
                linha = f"• [{ad['title'][:40]}]({ad['url']}) - *{olx_ro.format_price(ad['price'])}* (Nota {ad['score']}/10)"
                if "ram" in t or "ddr" in t or "memória" in t or "memoria" in t:
                    ram_list.append(linha)
                elif "ssd" in t or "sata" in t:
                    ssd_list.append(linha)
                elif any(g in t for g in ["gtx", "rx", "placa de vídeo", "placa de video", "geforce", "radeon", "rtx"]):
                    gpu_list.append(linha)
                elif any(g in t for g in ["vita", "3ds", "2ds", "psp", "switch", "nintendo", "playstation", "xbox", "game boy", "gameboy", "console"]):
                    games_list.append(linha)
                else:
                    outros_list.append(linha)
                    
            msg = "🏆 *REZUMATUL CLASAMENTULUI - CELE MAI BUNE OFERTE*\n\n"
            if ram_list:
                msg += "🧠 *MEMORIE RAM:*\n" + "\n".join(ram_list[:4]) + "\n\n"
            if ssd_list:
                msg += "💾 *SSD SATA:*\n" + "\n".join(ssd_list[:4]) + "\n\n"
            if gpu_list:
                msg += "🎮 *PLĂCI VIDEO:*\n" + "\n".join(gpu_list[:4]) + "\n\n"
            if games_list:
                msg += "🕹️ *CONSOLE ȘI PORTABILE (TIER S/A):*\n" + "\n".join(games_list[:4]) + "\n\n"
            if outros_list:
                msg += "📦 *ALTE PRODUSE:*\n" + "\n".join(outros_list[:4]) + "\n\n"
                
            msg += "_Rezumat automat, generat la fiecare 15 oferte analizate._"
            send_msg(chat_id, msg)
    else:
        c.execute("UPDATE bot_state SET value = ? WHERE key = 'ads_since_last_digest'", (count,))
        conn.commit()
        conn.close()


# ================= BOT TELEGRAM INTERATIVO =================
def processar_comandos_telegram():
    last_update_id = 0
    print("Ascult mesajele primite pe Telegram...")

    while True:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
            params = {"offset": last_update_id + 1, "timeout": 20}
            r = requests.get(url, params=params, timeout=25)

            if r.status_code != 200:
                continue

            for update in r.json().get("result", []):
                last_update_id = update["update_id"]

                # Butoane inline (ștergere din listă)
                callback = update.get("callback_query")
                if callback:
                    chat_id = str(callback.get("message", {}).get("chat", {}).get("id", ""))
                    data = (callback.get("data") or "").strip()
                    callback_id = callback.get("id")
                    if not chat_id or not data:
                        continue
                    if data.startswith("del:"):
                        try:
                            del_id = int(data.split(":", 1)[1])
                        except ValueError:
                            answer_callback(callback_id, "ID invalid")
                            continue
                        delete_search(chat_id, del_id)
                        pending_actions.pop(chat_id, None)
                        answer_callback(callback_id, "Șters")
                    continue

                msg = update.get("message", {})
                chat_id = str(msg.get("chat", {}).get("id", ""))
                texto = (msg.get("text") or "").strip()
                if not chat_id or not texto:
                    continue

                texto = re.sub(r"@[a-zA-Z0-9_]+bot\b", "", texto, flags=re.IGNORECASE).strip()
                texto_norm = texto.lower().strip()
                print(f"Mesaj de la {chat_id}: '{texto}'")

                # Anulare pas intermediar
                if texto_norm in {"anulează", "anuleaza", "cancel", "/cancel"}:
                    pending_actions.pop(chat_id, None)
                    send_msg(chat_id, "OK, am anulat.", reply_markup=MAIN_KEYBOARD)
                    continue

                button_action = BUTTON_ALIASES.get(texto_norm)

                # Dacă aștept modelul după butonul Adaugă
                pending = pending_actions.get(chat_id)
                if pending and pending.get("action") == "add" and not button_action:
                    termo, preco = parse_add_input(texto)
                    if not termo:
                        send_msg(
                            chat_id,
                            "Scrie modelul și prețul, ex: `honor 600 1700`",
                            reply_markup=MAIN_KEYBOARD,
                        )
                        continue
                    add_search(chat_id, termo, preco)
                    pending_actions.pop(chat_id, None)
                    continue

                # Dacă aștept ID pentru ștergere
                if pending and pending.get("action") == "del" and not button_action:
                    if texto.isdigit():
                        delete_search(chat_id, int(texto))
                        pending_actions.pop(chat_id, None)
                    else:
                        send_msg(
                            chat_id,
                            "Alege un buton ❌ sau scrie ID-ul numeric. `anulează` ca să renunți.",
                        )
                    continue

                # Butoane / comenzi
                if (
                    button_action == "help"
                    or texto_norm in {"/start", "/help", "ajuda", "salut", "oi", "ola"}
                ):
                    pending_actions.pop(chat_id, None)
                    send_help(chat_id)
                    continue

                if button_action == "add" or texto_norm in {"/add", "/monitorar"}:
                    pending_actions[chat_id] = {"action": "add"}
                    send_msg(
                        chat_id,
                        "✍️ Scrie *modelul exact* și prețul maxim.\n\n"
                        "Exemple:\n"
                        "• `honor 600 1700`\n"
                        "• `jbl 310 310`\n"
                        "• `iphone 12 1200`\n\n"
                        "Trimite `anulează` ca să renunți.",
                        reply_markup=MAIN_KEYBOARD,
                    )
                    continue

                if button_action == "list" or texto_norm.startswith("/list") or "căutările mele" in texto_norm:
                    pending_actions.pop(chat_id, None)
                    list_searches(chat_id)
                    continue

                if button_action == "del" or texto_norm in {"/del", "/remover", "/șterge", "/sterge"}:
                    prompt_delete_searches(chat_id)
                    continue

                if texto_norm.startswith("/del") or texto_norm.startswith("/remover") or texto_norm.startswith("/șterge"):
                    partes = texto.split()
                    if len(partes) > 1 and partes[1].isdigit():
                        pending_actions.pop(chat_id, None)
                        delete_search(chat_id, int(partes[1]))
                    else:
                        prompt_delete_searches(chat_id)
                    continue

                if button_action == "tierlist" or texto_norm.startswith("/tierlist") or "tier list" in texto_norm:
                    pending_actions.pop(chat_id, None)
                    show_tierlist(chat_id)
                    continue

                if (
                    button_action == "top"
                    or texto_norm.startswith("/top")
                    or texto_norm.startswith("/tiers")
                    or "cele mai bune oferte" in texto_norm
                ):
                    pending_actions.pop(chat_id, None)
                    show_top(chat_id)
                    continue

                if texto.startswith("/add") or texto.startswith("/monitorar"):
                    rest = texto.split(maxsplit=1)
                    if len(rest) > 1:
                        termo, preco = parse_add_input(rest[1])
                        if termo:
                            pending_actions.pop(chat_id, None)
                            add_search(chat_id, termo, preco)
                            continue
                    pending_actions[chat_id] = {"action": "add"}
                    send_msg(
                        chat_id,
                        "✍️ Scrie modelul și prețul, ex: `honor 600 1700`",
                        reply_markup=MAIN_KEYBOARD,
                    )
                    continue

                # Limbaj natural (Gemini)
                interpretacao = interpretar_intencao_usuario(texto)
                acao = interpretacao.get("acao")
                if acao == "adicionar" and interpretacao.get("termo"):
                    pending_actions.pop(chat_id, None)
                    add_search(
                        chat_id,
                        interpretacao["termo"],
                        interpretacao.get("preco_maximo") or 999999.0,
                    )
                elif acao == "listar":
                    list_searches(chat_id)
                elif acao == "remover":
                    prompt_delete_searches(chat_id)
                else:
                    send_msg(
                        chat_id,
                        interpretacao.get(
                            "resposta",
                            "Nu am înțeles. Apasă *➕ Adaugă* și scrie modelul (ex: `jbl 310 310`).",
                        ),
                        reply_markup=MAIN_KEYBOARD,
                    )

        except Exception as e:
            print(f"Eroare în bucla de mesaje Telegram: {e}")
            time.sleep(3)


# ================= LOOP PRINCIPAL =================
def loop_scraping():
    while True:
        try:
            monitorar_rodada()
        except Exception as e:
            print(f"Eroare în runda de monitorizare: {e}")
        time.sleep(INTERVALO_SEGUNDOS)

if __name__ == "__main__":
    init_db()
    # Ascultă mesajele Telegram într-un fir de execuție separat.
    t_telegram = threading.Thread(target=processar_comandos_telegram, daemon=True)
    t_telegram.start()
    
    # Pornește monitorizarea continuă.
    loop_scraping()
