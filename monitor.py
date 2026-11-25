import os
import re
import json
import html
import sqlite3
import sys
import time
import threading
from contextlib import contextmanager
from datetime import datetime
import requests
from curl_cffi import requests as cffi_requests
from bs4 import BeautifulSoup
from google import genai
import benchmarks
import market_analytics
import olx_ro
import vinted_ro

from dotenv import load_dotenv

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="backslashreplace")

# Carrega configurações e segredos do arquivo .env
load_dotenv()

# ================= CONFIGURAÇÕES =================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DB_PATH = os.getenv("DB_PATH", os.path.join(os.path.dirname(__file__), "ofertas_ro.db"))
INTERVALO_SEGUNDOS = int(os.getenv("INTERVALO_SEGUNDOS", "45"))
# OLX.ro nu afișează mai mult de 25 de pagini; pagina următoare revine la prima.
MAX_PAGES_PER_SEARCH = int(os.getenv("MAX_PAGES_PER_SEARCH", "25"))
# Scan rapid: doar pagina 1 („Noi”); la fiecare N runde → scan profund (toate paginile).
MAX_PAGES_RAPID = int(os.getenv("MAX_PAGES_RAPID", "1"))
DEEP_EVERY_N_ROUNDS = int(os.getenv("DEEP_EVERY_N_ROUNDS", "10"))
SEARCH_PAGE_DELAY_SECONDS = 2
# Alertă doar dacă marja netă (după cost flip) ≥ acest prag.
MIN_MARJA_LEI = float(os.getenv("MIN_MARJA_LEI", "200"))
COST_FLIP_LEI = float(os.getenv("COST_FLIP_LEI", "25"))
VINTED_SHIPPING_LEI = float(
    os.getenv("VINTED_SHIPPING_LEI", str(getattr(vinted_ro, "SHIPPING_LEI", 20)))
)
vinted_ro.SHIPPING_LEI = VINTED_SHIPPING_LEI
OLX_IMPERSONATE = os.getenv("OLX_IMPERSONATE", "chrome120")
OLX_ACCESS_BLOCKED = False
_SCRAPE_ROUND = 0
_DB_LOCK = threading.RLock()
WATCH_PRICE_DROP_LEI = 0.5
# Stare conversație pe chat: după butonul Adaugă, așteaptă modelul scris de utilizator.
pending_actions = {}


def parse_allowed_chat_ids(raw):
    """Listă CSV/`;` de chat_id Telegram autorizate. Gol = nimeni (fail-closed)."""
    if not raw or not str(raw).strip():
        return frozenset()
    ids = set()
    for part in str(raw).replace(";", ",").split(","):
        part = part.strip()
        if part:
            ids.add(part)
    return frozenset(ids)


TELEGRAM_ALLOWED_CHAT_IDS = parse_allowed_chat_ids(
    os.getenv("TELEGRAM_ALLOWED_CHAT_IDS", "")
)


def chat_is_allowed(chat_id):
    return str(chat_id) in TELEGRAM_ALLOWED_CHAT_IDS

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
    # impersonate setează amprenta TLS (JA3) și HTTP/2 ale browserului,
    # inclusiv User-Agent-ul corespunzător. Nu trimitem un UA separat.
    response = cffi_requests.get(
        url,
        impersonate=OLX_IMPERSONATE,
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
    """Deschide o conexiune SQLite. Pentru uz multi-thread folosește db_session()."""
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


@contextmanager
def db_session():
    """Serializează citirile/scrierile între firul Telegram și cel de scanare."""
    with _DB_LOCK:
        conn = get_db()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def init_db():
    with db_session() as conn:
        c = conn.cursor()
        c.execute("PRAGMA journal_mode=WAL")
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
        ad_columns = {row["name"] for row in c.execute("PRAGMA table_info(ads)")}
        if "source" not in ad_columns:
            c.execute("ALTER TABLE ads ADD COLUMN source TEXT NOT NULL DEFAULT 'olx'")
        c.execute("UPDATE ads SET source = 'vinted' WHERE id LIKE 'vinted:%'")

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
        try:
            c.execute("ALTER TABLE searches ADD COLUMN source TEXT DEFAULT 'olx'")
        except Exception:
            pass
        c.execute(
            "INSERT OR IGNORE INTO bot_state (key, value) VALUES ('ads_since_last_digest', 0)"
        )
        note_columns = {
            row["name"] for row in c.execute("PRAGMA table_info(sent_notifications)")
        }
        if "status" not in note_columns:
            c.execute(
                "ALTER TABLE sent_notifications ADD COLUMN status TEXT DEFAULT 'sent'"
            )
        if "seen_price" not in note_columns:
            c.execute(
                "ALTER TABLE sent_notifications ADD COLUMN seen_price REAL"
            )


def ad_ja_notificado(ad_id, chat_id):
    """True doar pentru stări închise (sent/rejected). watch poate fi redeschis."""
    stare = obtine_stare(ad_id, chat_id)
    if not stare:
        return False
    return stare[0] in {"sent", "rejected"}


def registrar_notificacao(ad_id, chat_id, price=None):
    """Compat: marchează ca trimis definitiv."""
    salveaza_stare(ad_id, chat_id, "sent", price)


def salveaza_stare(ad_id, chat_id, status, price=None):
    with db_session() as conn:
        c = conn.cursor()
        c.execute(
            """
            INSERT INTO sent_notifications (ad_id, chat_id, status, seen_price)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(ad_id, chat_id) DO UPDATE SET
                status = excluded.status,
                seen_price = excluded.seen_price
            """,
            (ad_id, str(chat_id), status, price),
        )


def obtine_stare(ad_id, chat_id):
    with db_session() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT status, seen_price FROM sent_notifications WHERE ad_id = ? AND chat_id = ?",
            (ad_id, str(chat_id)),
        )
        row = c.fetchone()
        if not row:
            return None
        return (row["status"] or "sent", row["seen_price"])


def should_reopen_watch(seen_price, current_price, force_reeval=False):
    """
    watch se redeschide la scădere de preț sau la reevaluare forțată
    (ex. rundă deep, când pot apărea date OLX noi pentru mediană).
    """
    if force_reeval:
        return True
    if seen_price is None or current_price is None:
        return True
    return float(current_price) < float(seen_price) - WATCH_PRICE_DROP_LEI


def is_deep_round(round_no=None):
    n = _SCRAPE_ROUND if round_no is None else int(round_no)
    return n > 0 and n % DEEP_EVERY_N_ROUNDS == 0


def salvar_ad_cache(ad_id, title, price, url, score=None, review=None, tier=None, pitaco_benchmark=None, pitaco_mercado=None, source="olx"):
    with db_session() as conn:
        c = conn.cursor()
        c.execute(
            """
            INSERT INTO ads
            (id, title, price, url, score, review, tier, benchmark_review, market_review, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                title = excluded.title,
                price = excluded.price,
                url = excluded.url,
                score = excluded.score,
                review = excluded.review,
                tier = excluded.tier,
                benchmark_review = excluded.benchmark_review,
                market_review = excluded.market_review,
                source = excluded.source
            """,
            (
                ad_id,
                title,
                price,
                url,
                score,
                review,
                tier,
                pitaco_benchmark,
                pitaco_mercado,
                source,
            ),
        )


def obter_ad_cache(ad_id):
    with db_session() as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM ads WHERE id = ?", (ad_id,))
        return c.fetchone()


# ================= AVALIAÇÃO COM IA MULTI-AGENTE =================
def avaliar_com_ia(title, description, price, query, max_price=None, sursa="OLX", profil=None):
    hw_nome, hw_tipo, hw_info = benchmarks.identifica_hardware(title)
    stats_mercado = market_analytics.obtine_statistici_piata(
        query, price, pret_maxim=max_price, stocare=(profil or {}).get("storage")
    )
    if profil:
        stats_mercado = olx_ro.bind_profile(stats_mercado, profil)
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

    origine = (
        "Anunțul este de pe Vinted. Revânzarea se face pe OLX România; nu schimba medianele date."
        if sursa == "Vinted"
        else "Anunțul este de pe OLX România."
    )
    profil = profil or {}
    prompt = f"""Ești evaluator de oferte second-hand, specializat pe TELEFOANE pentru arbitraj (cumpăr ieftin → revând cu profit).
Scrie toate textele exclusiv în limba română.
Bazează concluzia pe datele numerice furnizate și nu le contrazice.
{origine}

ANUNȚ EVALUAT:
- Căutare: "{query}"
- Titlu: "{title}"
- Preț: {olx_ro.format_price(price)}
- Descriere: {description}
- Stocare identificată: {profil.get("storage_label") or "necunoscută"}
- Stare: {profil.get("condition") or "necunoscut"}
- Variantă: {profil.get("variant") or "standard"}

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
- Potrivire STRICTĂ pe modelul din căutare: „Honor 600” ≠ Smart/Pro/Lite/smart5g; „iPhone 12” ≠ 12 Pro/Pro Max/Mini; „S23” ≠ S23 Ultra/FE. Dacă titlul sau descrierea sugerează altă variantă → aprovado=false, nota=0.
- Respinge accesorii (husă, folie, încărcător), piese, telefoane defecte, iCloud/Google lock, preț 1 leu, schimburi fără preț clar.
- Nu inventa baterie %, stocare sau garanții dacă nu apar în anunț. Dacă stocarea sau starea sunt „necunoscută” / „necunoscut”, nu le inventa.
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
def send_telegram(chat_id, title, price, url, description, score, review, pitaco_benchmark=None, pitaco_mercado=None, stats_mercado=None, image_url=None, sursa="OLX"):
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
        marja_neta = market_analytics.marja_neta_lei(stats_mercado, COST_FLIP_LEI)
        pret_revanzare = stats_mercado.get("pret_revanzare_rapida")
        profil_for_note = stats_mercado.get("profil") or {}
        cohort_bits = []
        if profil_for_note.get("variant"):
            cohort_bits.append(str(profil_for_note["variant"]))
        if stats_mercado.get("cohort_specific") and profil_for_note.get("storage"):
            cohort_bits.append(str(profil_for_note.get("storage_label")))
        cohort_note = f", varianta {' · '.join(cohort_bits)}" if cohort_bits else ""
        mercado_linha = (
            f"📊 <b>Comparație cu piața:</b>\n"
            f"• {reference_label}: {olx_ro.format_price(stats_mercado['mediana'])} ({sample_label}{cohort_note})\n"
            f"• Prețul este {market_position}.\n"
            f"💵 <b>Marjă netă est.:</b> {olx_ro.format_price(marja_neta)}"
            f" (revânzare ~{olx_ro.format_price(pret_revanzare)}, cost flip {olx_ro.format_price(COST_FLIP_LEI)})\n"
        )

    profil = (stats_mercado or {}).get("profil") or {}
    profil_linha = ""
    if profil:
        profil_linha = (
            f"💾 <b>Stocare:</b> {html.escape(str(profil.get('storage_label') or 'necunoscută'))}\n"
            f"🏷️ <b>Stare:</b> {html.escape(str(profil.get('condition') or 'necunoscut'))}\n"
            f"🔀 <b>Variantă:</b> {html.escape(str(profil.get('variant') or 'standard'))}\n"
            f"🔒 <b>Încredere:</b> {int(profil.get('incredere') or 0)}/100\n"
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
        f"{profil_linha}"
        f"{mercado_linha}"
        f"🧠 <b>Concluzie:</b> <i>{html.escape(review)}</i>\n"
        f"{pareceres}\n"
        f"🔗 <a href=\"{html.escape(url, quote=True)}\">Deschide anunțul pe {html.escape(sursa)}</a>\n\n"
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


def parse_search_request(texto):
    """`vinted iphone 13 1200` → sursă vinted; restul rămâne OLX."""
    body = texto.strip()
    source = "olx"
    if body.lower().startswith("vinted "):
        source = "vinted"
        body = body.split(maxsplit=1)[1].strip()
    termo, preco = parse_add_input(body)
    return source, termo, preco


def add_search(chat_id, termo, preco, source="olx"):
    source = "vinted" if source == "vinted" else "olx"
    with db_session() as conn:
        c = conn.cursor()
        c.execute(
            "INSERT INTO searches (chat_id, query, max_price, source) VALUES (?, ?, ?, ?)",
            (chat_id, termo, preco, source),
        )
    preco_msg = olx_ro.format_price(preco) if preco < 999999 else "Fără limită de preț"
    if source == "vinted":
        send_msg(
            chat_id,
            f"🛍 *Căutare Vinted adăugată!*\n\n📦 *Produs:* `{termo}`\n💰 *Preț maxim:* {preco_msg}\n"
            "Alertă la anunț nou, model exact, cu cel puțin 20% sub mediana paginii.",
            reply_markup=MAIN_KEYBOARD,
        )
        return
    send_msg(
        chat_id,
        f"🎯 *Căutare adăugată!*\n\n📦 *Produs:* `{termo}`\n💰 *Preț maxim:* {preco_msg}\n"
        f"💵 Alertă doar la marjă netă ≥ {olx_ro.format_price(MIN_MARJA_LEI)} "
        f"(model exact; dacă pui `128`/`256` în căutare, filtrez stocarea).\n",
        reply_markup=MAIN_KEYBOARD,
    )


def list_searches(chat_id):
    with db_session() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT id, query, max_price, source FROM searches WHERE chat_id = ?",
            (chat_id,),
        )
        rows = c.fetchall()

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
        where = "Vinted" if row["source"] == "vinted" else "OLX"
        resp += f"*{i}.* [{where}] `{row['query']}` (până la {preco_str}) — id `{row['id']}`\n"
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
        "Exemple: `iphone 13 128 1800`, `samsung s23 1500`\n"
        "Vinted: `vinted iphone 13 1200` sau `/vinted iphone 13 1200`\n"
        "Filtru: model exact · stocare dacă o pui (`128`≠`256`) · fără piese/lock\n"
        f"Alertă doar la marjă netă ≥ {olx_ro.format_price(MIN_MARJA_LEI)}."
    )
    send_msg(chat_id, help_msg, reply_markup=MAIN_KEYBOARD)


def show_tierlist(chat_id):
    with db_session() as conn:
        c = conn.cursor()
        c.execute("SELECT query, max_price FROM searches WHERE chat_id = ?", (chat_id,))
        buscas_usuario = c.fetchall()

        if not buscas_usuario:
            buscas_usuario = []
            resp = None
        else:
            resp = "📊 *LISTA TA DE OFERTE*\n\n"
            for b in buscas_usuario:
                termo = b["query"]
                preco_limite = b["max_price"] or 999999.0
                palavras = [p for p in termo.split() if len(p) > 2]
                where_clauses = (
                    " AND ".join(["LOWER(title) LIKE ?" for _ in palavras]) or "1=1"
                )
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

    if not buscas_usuario:
        send_msg(
            chat_id,
            "Nu ai căutări active. Apasă *➕ Adaugă* și scrie modelul.",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    resp += "_Doar modelul exact din căutările tale._"
    send_msg(chat_id, resp, reply_markup=MAIN_KEYBOARD)


def show_top(chat_id):
    with db_session() as conn:
        c = conn.cursor()
        c.execute(
            """
            SELECT title, price, score, review, url
            FROM ads
            WHERE score >= 8
              AND COALESCE(source, 'olx') = 'olx'
              AND id NOT LIKE 'vinted:%'
            ORDER BY score DESC, created_at DESC
            LIMIT 8
            """
        )
        rows = c.fetchall()

    if not rows:
        send_msg(
            chat_id,
            "Nu am găsit încă oferte OLX cu scor mare (8–10).",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    resp = "🏆 *CELE MAI BUNE OFERTE OLX (8–10/10):*\n\n"
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
    with db_session() as conn:
        c = conn.cursor()
        c.execute(
            "DELETE FROM searches WHERE id = ? AND chat_id = ?",
            (del_id, chat_id),
        )
        deleted = conn.total_changes > 0
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


def fetch_item(url):
    """Descarcă anunțul OLX și verifică descrierea (stocare, stare, variantă)."""
    details = extrair_detalhes_ad(url)
    if not details:
        return None
    details["profil"] = olx_ro.inspect_listing(
        details.get("title") or "",
        details.get("description") or "",
        description_fetched=True,
    )
    return details


def monitorar_rodada(max_pages=None):
    global OLX_ACCESS_BLOCKED
    if OLX_ACCESS_BLOCKED:
        return

    pages_limit = max_pages if max_pages is not None else MAX_PAGES_PER_SEARCH
    print(
        f"[{datetime.now().strftime('%H:%M:%S')}] "
        f"Încep scanarea ({'profund' if pages_limit > 1 else 'rapid'}, max {pages_limit} pag.)..."
    )
    with db_session() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT id, chat_id, query, max_price FROM searches "
            "WHERE COALESCE(source, 'olx') = 'olx'"
        )
        buscas = c.fetchall()

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
            for page in range(1, pages_limit + 1):
                url_busca = olx_ro.build_search_url(query, page)
                try:
                    r = fetch_olx_page(url_busca, timeout=15)
                except cffi_requests.exceptions.HTTPError as error:
                    if error.response is not None and error.response.status_code == 404:
                        break
                    raise
                if r.status_code != 200:
                    break
                if page > 1 and f"page={page}" not in str(getattr(r, "url", url_busca)):
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
                links_ad.extend(new_page_links)
                print(
                    f"  {query}: pagina {page}, {len(new_page_links)} anunțuri noi "
                    f"({len(links_ad)} în total)"
                )
                if page < pages_limit:
                    time.sleep(SEARCH_PAGE_DELAY_SECONDS)

            force_reeval = is_deep_round()
            # Procesează toate anunțurile găsite, de la cele mai noi.
            for ad_id, href in links_ad:
                stare = obtine_stare(ad_id, chat_id)
                if stare and stare[0] in {"sent", "rejected"}:
                    continue

                profil = None
                marja_neta = 0.0
                # Cache doar pentru anunțuri noi (fără stare); watch forțează refetch (preț).
                cached = None if stare else obter_ad_cache(ad_id)
                if cached and cached["score"] is not None:
                    titulo = cached["title"]
                    preco = cached["price"]
                    desc = cached["review"] if cached["review"] else ""
                    if not olx_ro.listing_matches_exact_model(query, titulo, ""):
                        salveaza_stare(ad_id, chat_id, "rejected", preco)
                        continue
                    desc = "Anunț verificat în cache."
                    score = cached["score"]
                    parecer = cached["review"]
                    tier = cached["tier"] if "tier" in cached.keys() else "Tier B"
                    pitaco_benchmark = (
                        cached["benchmark_review"]
                        if "benchmark_review" in cached.keys()
                        else None
                    )
                    pitaco_mercado = (
                        cached["market_review"]
                        if "market_review" in cached.keys()
                        else None
                    )
                    stats_mercado = None
                    img = None
                    profil = olx_ro.inspect_listing(titulo, "", description_fetched=False)
                else:
                    detalhes = fetch_item(href)
                    if not detalhes:
                        salvar_ad_cache(ad_id, "Necunoscut", 0, href)
                        continue

                    titulo = detalhes["title"]
                    preco = detalhes["price"]
                    desc = detalhes["description"]
                    img = detalhes["image_url"]
                    profil = detalhes.get("profil")

                    if stare and stare[0] == "watch" and not should_reopen_watch(
                        stare[1], preco, force_reeval=force_reeval
                    ):
                        continue

                    if profil and not profil.get("ok", True):
                        salvar_ad_cache(
                            ad_id,
                            titulo,
                            preco,
                            href,
                            score=0,
                            review=profil.get("motiv") or "Descrierea contrazice titlul",
                        )
                        salveaza_stare(ad_id, chat_id, "rejected", preco)
                        continue

                    if not olx_ro.listing_matches_exact_model(query, titulo, desc):
                        salvar_ad_cache(
                            ad_id,
                            titulo,
                            preco,
                            href,
                            score=0,
                            review="Model diferit de căutarea exactă",
                        )
                        salveaza_stare(ad_id, chat_id, "rejected", preco)
                        continue

                    if market_analytics.este_cautare_telefon(query):
                        risc = market_analytics.text_are_risc_flip(titulo, desc)
                        if risc:
                            salvar_ad_cache(
                                ad_id,
                                titulo,
                                preco,
                                href,
                                score=0,
                                review=risc,
                            )
                            salveaza_stare(ad_id, chat_id, "rejected", preco)
                            continue

                    if preco <= 5:
                        salvar_ad_cache(
                            ad_id, titulo, preco, href, score=0, review="Preț invalid"
                        )
                        salveaza_stare(ad_id, chat_id, "rejected", preco)
                        continue

                    if preco > teto_preco:
                        salvar_ad_cache(
                            ad_id,
                            titulo,
                            preco,
                            href,
                            score=0,
                            review="Preț în afara limitei",
                        )
                        # watch: o reducere ulterioară redeschide anunțul
                        salveaza_stare(ad_id, chat_id, "watch", preco)
                        continue

                    print(
                        f"🧠 Analiză AI pentru [{chat_id}]: '{titulo}' "
                        f"la {olx_ro.format_price(preco)}..."
                    )
                    avaliacao = avaliar_com_ia(
                        titulo, desc, preco, query, teto_preco, profil=profil
                    )
                    score = avaliacao["nota"]
                    parecer = avaliacao["parecer"]
                    aprovado = avaliacao["aprovado"]
                    tier = avaliacao.get("tier", "Tier B")
                    pitaco_benchmark = avaliacao.get("pitaco_benchmark")
                    pitaco_mercado = avaliacao.get("pitaco_mercado")
                    stats_mercado = avaliacao.get("stats_mercado")

                    if not aprovado:
                        score = 0

                    salvar_ad_cache(
                        ad_id,
                        titulo,
                        preco,
                        href,
                        score,
                        parecer,
                        tier,
                        pitaco_benchmark,
                        pitaco_mercado,
                    )

                desc_for_match = "" if desc == "Anunț verificat în cache." else desc

                if score >= 6 and not olx_ro.listing_matches_exact_model(
                    query, titulo, desc_for_match
                ):
                    print(f"⏭ Skip (model ≠ căutare): {titulo}")
                    score = 0

                if score >= 6 and market_analytics.este_cautare_telefon(query):
                    risc = market_analytics.text_are_risc_flip(titulo, desc_for_match)
                    if risc:
                        print(f"⏭ Skip ({risc}): {titulo}")
                        score = 0

                if score >= 6 and preco <= teto_preco:
                    if not stats_mercado or not stats_mercado.get("mediana"):
                        stats_mercado = market_analytics.obtine_statistici_piata(
                            query,
                            preco,
                            teto_preco,
                            stocare=(profil or {}).get("storage"),
                        )
                    if profil:
                        stats_mercado = olx_ro.bind_profile(stats_mercado, profil)
                    marja_neta = market_analytics.marja_neta_lei(
                        stats_mercado, COST_FLIP_LEI
                    )
                    if not stats_mercado.get("esantion_suficient"):
                        print(
                            f"⏭ Skip (prea puține anunțuri OLX comparabile): {titulo}"
                        )
                        score = 0
                    elif marja_neta < MIN_MARJA_LEI:
                        print(
                            f"⏭ Skip (marjă {olx_ro.format_price(marja_neta)} "
                            f"< min {olx_ro.format_price(MIN_MARJA_LEI)}): {titulo}"
                        )
                        score = 0

                if score >= 6 and preco <= teto_preco:
                    print(
                        f"🎯 Trimit notificarea către {chat_id}: {titulo} "
                        f"[{score}/10 - {tier}] marjă~{olx_ro.format_price(marja_neta)}"
                    )
                    send_telegram(
                        chat_id,
                        titulo,
                        preco,
                        href,
                        desc,
                        score,
                        parecer,
                        pitaco_benchmark,
                        pitaco_mercado,
                        stats_mercado,
                        img,
                    )
                    verificar_e_enviar_digest_periodico(chat_id)
                    salveaza_stare(ad_id, chat_id, "sent", preco)
                else:
                    # Scor/marjă insuficiente: urmărește reduceri / date OLX noi
                    salveaza_stare(ad_id, chat_id, "watch", preco)
                time.sleep(1.5)
                
        except OLXAccessDeniedError as e:
            print(e)
            OLX_ACCESS_BLOCKED = True
            return
        except Exception as e:
            print(f"Eroare la căutarea „{query}”: {e}")
            
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Scanarea s-a încheiat.")


def monitorar_vinted():
    with db_session() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT id, chat_id, query, max_price FROM searches WHERE source = 'vinted'"
        )
        buscas = c.fetchall()
    if not buscas:
        return

    print(f"[{datetime.now().strftime('%H:%M:%S')}] Scanare Vinted ({len(buscas)} căutări)...")
    for b in buscas:
        chat_id = str(b["chat_id"])
        query = b["query"]
        teto = float(b["max_price"] or 999999)
        try:
            response = vinted_ro.fetch_catalog(query, page=1)
            if response.status_code != 200:
                print(f"  Vinted {query}: HTTP {response.status_code}")
                continue
            items = vinted_ro.parse_catalog(response.text)
            classified = vinted_ro.classify_listings(items, query, teto)
            deals = {deal["id"]: deal for deal in classified["deals"]}
            comparable = {item["id"]: item for item in classified["comparable"]}
            median = classified["median"]
            force_reeval = is_deep_round()
            print(
                f"  Vinted {query}: {len(items)} anunțuri, "
                f"mediană {olx_ro.format_price(median) if median else 'n/a'} "
                f"({classified.get('median_basis', 'listed')}), "
                f"{len(deals)} sub 20%"
            )
            evaluated = 0
            for item in items:
                enriched = comparable.get(item["id"], item)
                cost = float(
                    enriched.get("acquisition") or vinted_ro.acquisition_cost(item)
                )
                stare = obtine_stare(item["id"], chat_id)
                if stare:
                    status, seen_price = stare
                    if status in {"sent", "rejected"}:
                        continue
                    if status == "watch" and not should_reopen_watch(
                        seen_price, cost, force_reeval=force_reeval
                    ):
                        continue
                if item["id"] not in comparable:
                    salveaza_stare(item["id"], chat_id, "rejected", cost)
                    continue
                deal = deals.get(item["id"])
                if not deal:
                    salveaza_stare(item["id"], chat_id, "watch", cost)
                    continue
                if evaluated >= 4:
                    continue
                evaluated += 1
                title = deal["title"]
                details = vinted_ro.fetch_item(deal["url"]) or {}
                if details.get("title"):
                    title = details["title"]
                raw_description = (details.get("description") or "").strip()
                profil = olx_ro.inspect_listing(
                    title,
                    raw_description,
                    platform_condition=deal.get("condition") or "",
                    description_fetched=bool(details),
                )
                profil["suspect"] = bool(deal.get("suspect"))
                if details and not profil.get("ok", True):
                    print(f"⏭ Vinted descriere: {profil.get('motiv')} — {title}")
                    salveaza_stare(deal["id"], chat_id, "rejected", cost)
                    continue
                if (
                    vinted_ro._is_accessory(query, title)
                    or not olx_ro.listing_matches_exact_model(
                        query, title, raw_description
                    )
                ):
                    print(f"⏭ Vinted model ≠ căutare: {title}")
                    salveaza_stare(deal["id"], chat_id, "rejected", cost)
                    continue
                desc = raw_description
                if deal.get("condition"):
                    desc = f"Stare: {deal['condition']}. {desc}".strip()
                if deal.get("suspect"):
                    desc = (
                        f"Ofertă suspectă (mult sub mediana Vinted). {desc}".strip()
                    )
                unit = float(
                    deal.get("total_price") or deal.get("price") or cost
                )
                if VINTED_SHIPPING_LEI > 0:
                    desc = (
                        f"Cost achiziție {olx_ro.format_price(cost)} "
                        f"(inclus transport ~{olx_ro.format_price(VINTED_SHIPPING_LEI)}). "
                        f"{desc}"
                    ).strip()
                elif cost > unit:
                    desc = (
                        f"Preț listat {olx_ro.format_price(float(deal.get('price') or unit))}, "
                        f"cost cu protecție {olx_ro.format_price(cost)}. {desc}"
                    ).strip()
                if market_analytics.este_cautare_telefon(query):
                    risc = market_analytics.text_are_risc_flip(title, desc)
                    if risc:
                        print(f"⏭ Vinted ({risc}): {title}")
                        salveaza_stare(deal["id"], chat_id, "rejected", cost)
                        continue
                print(
                    f"🧠 Vinted AI [{chat_id}]: '{title}' "
                    f"cost {olx_ro.format_price(cost)} "
                    f"(mediană Vinted {olx_ro.format_price(median)})..."
                )
                try:
                    avaliacao = avaliar_com_ia(
                        title, desc, cost, query, teto, sursa="Vinted", profil=profil
                    )
                except Exception as error:
                    print(f"Eroare AI Vinted: {error}")
                    continue
                score = avaliacao["nota"]
                stats_mercado = avaliacao.get("stats_mercado") or {}
                if profil:
                    stats_mercado = olx_ro.bind_profile(stats_mercado, profil)
                marja_neta = market_analytics.marja_neta_lei(stats_mercado, COST_FLIP_LEI)
                if not stats_mercado.get("esantion_suficient"):
                    print(f"⏭ Vinted fără mediană OLX suficientă: {title}")
                    score = 0
                elif marja_neta < MIN_MARJA_LEI:
                    print(
                        f"⏭ Vinted marjă {olx_ro.format_price(marja_neta)} "
                        f"< min {olx_ro.format_price(MIN_MARJA_LEI)}: {title}"
                    )
                    score = 0
                if score >= 6 and cost <= teto:
                    print(
                        f"🎯 Vinted către {chat_id}: {title} "
                        f"[{score}/10] marjă~{olx_ro.format_price(marja_neta)}"
                    )
                    send_telegram(
                        chat_id,
                        title,
                        cost,
                        deal["url"],
                        desc,
                        score,
                        avaliacao["parecer"],
                        avaliacao.get("pitaco_benchmark"),
                        avaliacao.get("pitaco_mercado"),
                        stats_mercado,
                        deal.get("image_url"),
                        sursa="Vinted",
                    )
                    verificar_e_enviar_digest_periodico(chat_id)
                    salveaza_stare(deal["id"], chat_id, "sent", cost)
                else:
                    salveaza_stare(deal["id"], chat_id, "watch", cost)
                salvar_ad_cache(
                    deal["id"],
                    title,
                    cost,
                    deal["url"],
                    score,
                    avaliacao["parecer"],
                    avaliacao.get("tier"),
                    avaliacao.get("pitaco_benchmark"),
                    avaliacao.get("pitaco_mercado"),
                    source="vinted",
                )
                time.sleep(1)
        except Exception as e:
            print(f"Eroare Vinted „{query}”: {e}")
            vinted_ro.reset_session()
        time.sleep(2)

def verificar_e_enviar_digest_periodico(chat_id):
    """Generează un rezumat cu cele mai bune oferte la fiecare 15 notificări trimise."""
    with db_session() as conn:
        c = conn.cursor()
        c.execute("SELECT value FROM bot_state WHERE key = 'ads_since_last_digest'")
        row = c.fetchone()
        count = (row["value"] if row else 0) + 1

        if count >= 15:
            c.execute(
                "UPDATE bot_state SET value = 0 WHERE key = 'ads_since_last_digest'"
            )
            c.execute(
                """
                SELECT title, price, score, url
                FROM ads
                WHERE score >= 7
                  AND COALESCE(source, 'olx') = 'olx'
                  AND id NOT LIKE 'vinted:%'
                ORDER BY score DESC, created_at DESC
                LIMIT 15
                """
            )
            top_ads = c.fetchall()
        else:
            c.execute(
                "UPDATE bot_state SET value = ? WHERE key = 'ads_since_last_digest'",
                (count,),
            )
            top_ads = None

    if count < 15 or not top_ads:
        return

    ram_list = []
    ssd_list = []
    gpu_list = []
    games_list = []
    outros_list = []

    for ad in top_ads:
        t = ad["title"].lower()
        linha = (
            f"• [{ad['title'][:40]}]({ad['url']}) - "
            f"*{olx_ro.format_price(ad['price'])}* (Nota {ad['score']}/10)"
        )
        if "ram" in t or "ddr" in t or "memória" in t or "memoria" in t:
            ram_list.append(linha)
        elif "ssd" in t or "sata" in t:
            ssd_list.append(linha)
        elif any(
            g in t
            for g in [
                "gtx",
                "rx",
                "placa de vídeo",
                "placa de video",
                "geforce",
                "radeon",
                "rtx",
            ]
        ):
            gpu_list.append(linha)
        elif any(
            g in t
            for g in [
                "vita",
                "3ds",
                "2ds",
                "psp",
                "switch",
                "nintendo",
                "playstation",
                "xbox",
                "game boy",
                "gameboy",
                "console",
            ]
        ):
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


# ================= BOT TELEGRAM INTERATIVO =================
def processar_comandos_telegram():
    last_update_id = 0
    if TELEGRAM_ALLOWED_CHAT_IDS:
        print(
            f"Ascult Telegram (allowlist: {len(TELEGRAM_ALLOWED_CHAT_IDS)} chat-uri)..."
        )
    else:
        print(
            "Ascult Telegram, dar TELEGRAM_ALLOWED_CHAT_IDS e gol — "
            "toate comenzile sunt respinse (fail-closed)."
        )

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
                    if not chat_is_allowed(chat_id):
                        print(f"Callback neautorizat ignorat: {chat_id}")
                        answer_callback(callback_id, "Neautorizat")
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
                if not chat_is_allowed(chat_id):
                    print(f"Chat neautorizat ignorat: {chat_id}")
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
                    source, termo, preco = parse_search_request(texto)
                    if not termo:
                        send_msg(
                            chat_id,
                            "Scrie modelul și prețul, ex: `honor 600 1700`",
                            reply_markup=MAIN_KEYBOARD,
                        )
                        continue
                    add_search(chat_id, termo, preco, source)
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
                        "• `honor 600 1700` (OLX)\n"
                        "• `vinted iphone 13 1200`\n"
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

                if texto_norm.startswith("/vinted"):
                    rest = texto.split(maxsplit=1)
                    if len(rest) > 1:
                        source, termo, preco = parse_search_request("vinted " + rest[1])
                        if termo:
                            pending_actions.pop(chat_id, None)
                            add_search(chat_id, termo, preco, source)
                            continue
                    pending_actions[chat_id] = {"action": "add"}
                    send_msg(
                        chat_id,
                        "✍️ Scrie `vinted` + modelul + prețul, ex: `vinted iphone 13 1200`",
                        reply_markup=MAIN_KEYBOARD,
                    )
                    continue

                if texto.startswith("/add") or texto.startswith("/monitorar"):
                    rest = texto.split(maxsplit=1)
                    if len(rest) > 1:
                        source, termo, preco = parse_search_request(rest[1])
                        if termo:
                            pending_actions.pop(chat_id, None)
                            add_search(chat_id, termo, preco, source)
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
                    termo = interpretacao["termo"]
                    source = "olx"
                    if "vinted" in texto_norm:
                        source = "vinted"
                        termo = re.sub(r"\bvinted\b", "", termo, flags=re.IGNORECASE).strip()
                    add_search(
                        chat_id,
                        termo,
                        interpretacao.get("preco_maximo") or 999999.0,
                        source,
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
    global _SCRAPE_ROUND
    while True:
        try:
            _SCRAPE_ROUND += 1
            deep = _SCRAPE_ROUND % DEEP_EVERY_N_ROUNDS == 0
            pages = MAX_PAGES_PER_SEARCH if deep else MAX_PAGES_RAPID
            monitorar_rodada(max_pages=pages)
            monitorar_vinted()
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
