"""
Statistici de piață OLX — orientat pe telefoane (arbitraj / revânzare).
Calculează mediana, discountul, marja și lichiditatea pe modelul exact.
"""
import os
import sqlite3
import statistics

import olx_ro

# Lichiditate orientativă RO second-hand (euristici, nu garanții).
REFERINTA_LICHIDITATE = {
    # Telefoane — prioritate pentru fluxul actual
    "IPHONE": {
        "lichiditate": "Foarte ridicată",
        "timp_vanzare": "12–48 de ore",
        "cerere": "Cel mai lichid segment second-hand; Pro/Pro Max se mișcă cel mai repede",
    },
    "SAMSUNG": {
        "lichiditate": "Foarte ridicată",
        "timp_vanzare": "1–3 zile",
        "cerere": "Serie S/A cu cerere constantă; Ultra se vinde bine sub mediană",
    },
    "GALAXY": {
        "lichiditate": "Foarte ridicată",
        "timp_vanzare": "1–3 zile",
        "cerere": "Modele Galaxy lichide pe piața RO",
    },
    "HONOR": {
        "lichiditate": "Ridicată",
        "timp_vanzare": "2–5 zile",
        "cerere": "Cerere bună pe mid-range; verifică varianta exactă (Smart/Pro)",
    },
    "XIAOMI": {
        "lichiditate": "Ridicată",
        "timp_vanzare": "2–5 zile",
        "cerere": "Volum mare de anunțuri; marja vine din preț sub mediană",
    },
    "REDMI": {
        "lichiditate": "Ridicată",
        "timp_vanzare": "2–4 zile",
        "cerere": "Segment entry/mid foarte căutat",
    },
    "POCO": {
        "lichiditate": "Ridicată",
        "timp_vanzare": "2–5 zile",
        "cerere": "Bun raport preț/performanță; lichiditate bună",
    },
    "HUAWEI": {
        "lichiditate": "Medie spre ridicată",
        "timp_vanzare": "3–7 zile",
        "cerere": "Cerere ok, dar fără GMS poate încetini vânzarea",
    },
    "PIXEL": {
        "lichiditate": "Ridicată",
        "timp_vanzare": "2–5 zile",
        "cerere": "Nișă foto; se vinde bine dacă e curat și sub mediană",
    },
    "ONEPLUS": {
        "lichiditate": "Medie spre ridicată",
        "timp_vanzare": "3–6 zile",
        "cerere": "Public mai mic decât Samsung/iPhone",
    },
    "MOTOROLA": {
        "lichiditate": "Medie",
        "timp_vanzare": "4–8 zile",
        "cerere": "Prețuri mici, marje mici — caută discount clar",
    },
    "REALME": {
        "lichiditate": "Medie spre ridicată",
        "timp_vanzare": "3–6 zile",
        "cerere": "Mid-range accesibil, cerere moderată",
    },
    "OPPO": {
        "lichiditate": "Medie spre ridicată",
        "timp_vanzare": "3–6 zile",
        "cerere": "Cerere stabilă pe mid-range",
    },
    "VIVO": {
        "lichiditate": "Medie",
        "timp_vanzare": "4–8 zile",
        "cerere": "Lichiditate mai slabă decât Samsung/Xiaomi",
    },
    "NOTHING": {
        "lichiditate": "Medie",
        "timp_vanzare": "4–7 zile",
        "cerere": "Nișă; cumpără doar cu discount solid",
    },
    # Păstrate pentru GPU/console dacă mai apar căutări
    "PS VITA": {"lichiditate": "Foarte ridicată", "timp_vanzare": "24–48 de ore", "cerere": "Colecționari"},
    "NINTENDO 3DS": {"lichiditate": "Foarte ridicată", "timp_vanzare": "24–48 de ore", "cerere": "Retrogaming"},
    "NINTENDO SWITCH": {"lichiditate": "Foarte ridicată", "timp_vanzare": "12–36 de ore", "cerere": "Consolă hibrid foarte lichidă"},
    "GTX 1050": {"lichiditate": "Foarte ridicată", "timp_vanzare": "24–48 de ore", "cerere": "GPU entry"},
    "RTX 3060": {"lichiditate": "Ridicată", "timp_vanzare": "2–4 zile", "cerere": "GPU mid-range"},
}

TERMENI_IGNORA_PIESA = [
    "pc gamer",
    "calculator",
    "laptop",
    "unitate completa",
    "setup complet",
]

# Accesorii / piese / lock — nu sunt telefon flipabil.
TERMENI_IGNORA_TELEFON = [
    "doar husa",
    "doar husă",
    "doar folie",
    "folie sticla",
    "folie sticlă",
    "incarcator original",
    "încărcător original",
    "cablu usb",
    "pentru piese",
    "dezmembrari",
    "dezmembrări",
    "display nou",
    "ecran nou",
    "doar display",
    "doar ecran",
    "capac spate",
    "baterie noua",
    "baterie nouă",
    "placa de baza",
    "placă de bază",
    "icloud",
    "activation lock",
    "activ lock",
    "cont blocat",
    "google lock",
    "frp lock",
    "mdm",
    "nu porneste",
    "nu pornește",
    "display spart",
    "ecran spart",
    "geam spart",
    "pentru reparat",
    "defect major",
]


def text_are_risc_flip(title: str, description: str = "") -> str | None:
    """Returnează motivul de respingere sau None dacă e candidat OK."""
    blob = f"{title} {description or ''}".lower()
    for termen in TERMENI_IGNORA_TELEFON:
        if termen in blob:
            return f"Riscuri/piese/lock: {termen}"
    return None


def marja_neta_lei(stats_mercado: dict, cost_flip_lei: float = 25.0) -> float:
    """Marjă după costuri tipice (transport etc.)."""
    brut = float(stats_mercado.get("marja_profit") or 0)
    return round(brut - float(cost_flip_lei), 2)

MARCI_TELEFON = (
    "iphone",
    "samsung",
    "galaxy",
    "honor",
    "xiaomi",
    "redmi",
    "poco",
    "huawei",
    "pixel",
    "oneplus",
    "motorola",
    "realme",
    "oppo",
    "vivo",
    "nothing",
)


def este_cautare_telefon(query: str) -> bool:
    q = query.lower()
    return any(marca in q for marca in MARCI_TELEFON)


def obtine_statistici_piata(query, pret_anunt, pret_maxim=None, cale_db=None):
    if cale_db is None:
        cale_db = os.getenv(
            "DB_PATH", os.path.join(os.path.dirname(__file__), "ofertas_ro.db")
        )

    conn = sqlite3.connect(cale_db)
    c = conn.cursor()

    cuvinte = [cuvant for cuvant in query.split() if len(cuvant) > 2]
    clauze_where = ["LOWER(title) LIKE ?" for _ in cuvinte]
    params = [f"%{cuvant.lower()}%" for cuvant in cuvinte]

    q_lower = query.lower()
    este_componenta = any(
        x in q_lower for x in ["gtx", "rx", "rtx", "ram", "ddr3", "ddr4", "ssd", "placa"]
    )
    if este_componenta:
        for t_ignora in TERMENI_IGNORA_PIESA:
            clauze_where.append("LOWER(title) NOT LIKE ?")
            params.append(f"%{t_ignora}%")

    if este_cautare_telefon(query):
        for t_ignora in TERMENI_IGNORA_TELEFON:
            clauze_where.append("LOWER(title) NOT LIKE ?")
            params.append(f"%{t_ignora}%")

    where_str = " AND ".join(clauze_where) if clauze_where else "1=1"

    # Plafon mai generos la telefoane scumpe (ex. iPhone Pro Max).
    if pret_maxim and pret_maxim > 0:
        plafon_taiere = pret_maxim * 1.8
    elif pret_anunt > 0:
        plafon_taiere = pret_anunt * (2.2 if este_cautare_telefon(query) else 2.5)
    else:
        plafon_taiere = 8000.0 if este_cautare_telefon(query) else 5000.0

    c.execute(
        f"""
        SELECT title, price
        FROM ads
        WHERE {where_str}
          AND price >= 50
          AND price <= ?
        """,
        params + [plafon_taiere],
    )
    rows = c.fetchall()
    conn.close()

    preturi = sorted(
        [
            float(price)
            for title, price in rows
            if price is not None
            and olx_ro.title_matches_exact_model(query, title or "")
        ]
    )

    if len(preturi) < 3:
        # Estimare prudentă când istoricul e sărac (tipic la început pe un model nou).
        mediana = round(pret_anunt * 1.25, 2) if pret_anunt > 0 else 100.0
        total_esantioane = len(preturi)
    else:
        try:
            q1, q2, q3 = statistics.quantiles(preturi, n=4)
            iqr = q3 - q1
            limita_inf = max(50.0, q1 - 1.5 * iqr)
            limita_sup = q3 + 1.5 * iqr
            preturi_curate = [p for p in preturi if limita_inf <= p <= limita_sup]
            if not preturi_curate:
                preturi_curate = preturi
            mediana = round(statistics.median(preturi_curate), 2)
            total_esantioane = len(preturi_curate)
        except Exception:
            mediana = round(statistics.median(preturi), 2)
            total_esantioane = len(preturi)

    if mediana > 0:
        discount_pct = round(((mediana - pret_anunt) / mediana) * 100, 1)
    else:
        discount_pct = 0.0

    # Revânzare rapidă: telefoanele se mișcă mai bine aproape de mediană (92%).
    factor_revanzare = 0.92 if este_cautare_telefon(query) else 0.90
    pret_revanzare_rapida = round(mediana * factor_revanzare, 2)
    marja_profit = round(pret_revanzare_rapida - pret_anunt, 2)

    cheie_lichiditate = "Medie"
    timp_vanzare = "3–7 zile"
    q_upper = query.upper()
    # Marci mai lungi / specifice primul
    for item_key, info in sorted(
        REFERINTA_LICHIDITATE.items(), key=lambda x: len(x[0]), reverse=True
    ):
        if item_key in q_upper:
            cheie_lichiditate = info["lichiditate"]
            timp_vanzare = info["timp_vanzare"]
            break

    return {
        "mediana": mediana,
        "total_esantioane": total_esantioane,
        "discount_pct": discount_pct,
        "lichiditate": cheie_lichiditate,
        "timp_vanzare": timp_vanzare,
        "pret_revanzare_rapida": pret_revanzare_rapida,
        "marja_profit": marja_profit,
    }
