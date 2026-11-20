"""
Ground truth pentru evaluare: telefoane (prioritate), plus GPU/console păstrate.
"""
import re

# Checklist de flip second-hand pe brand (fără a inventa specificații de model).
TELEFOANE_DATABASE = {
    "IPHONE": {
        "segment": "Premium / foarte lichid",
        "puncte_atentie": (
            "baterie %, True Tone, Face ID, iCloud lock, istoric reparații, "
            "capacitate stocare, cutie/factură"
        ),
        "riscuri": "iCloud locked, baterie <85%, ecran schimbat non-OEM, IMEI blacklist",
        "sfat_revanzare": "Cele mai lichide: modele recente Pro/Pro Max sub mediana pieței",
    },
    "SAMSUNG": {
        "segment": "Premium–mid / lichid",
        "puncte_atentie": (
            "Knox/warranty bit, ecran (AMOLED burn-in), baterie, Google account lock, "
            "capacitate stocare"
        ),
        "riscuri": "Cont Google blocat, ecran aftermarket, telefon pe rate/furat",
        "sfat_revanzare": "Seriile S și A recente se mișcă rapid dacă prețul e sub mediană",
    },
    "HONOR": {
        "segment": "Mid-range / lichiditate bună",
        "puncte_atentie": "Varianta exactă (ex. 600 vs 600 Smart/Pro), baterie, cont, cutie",
        "riscuri": "Confuzie între variante de model; anunțuri magazin la preț de listă",
        "sfat_revanzare": "Cumpără doar varianta exactă căutată; marja vine din discount clar",
    },
    "XIAOMI": {
        "segment": "Mid-range / volum mare",
        "puncte_atentie": "Mi Account lock, stare ecran, baterie, stocare RAM/ROM",
        "riscuri": "Cont Mi blocat, variante Global vs China",
        "sfat_revanzare": "Preț agresiv sub mediană; concurență mare pe OLX",
    },
    "REDMI": {
        "segment": "Entry–mid / lichid",
        "puncte_atentie": "Mi Account, stare generală, stocare",
        "riscuri": "Cont blocat, confuzie Note/Pro/variante",
        "sfat_revanzare": "Marje mici — doar oferte clar sub piață",
    },
    "POCO": {
        "segment": "Mid-range performanță",
        "puncte_atentie": "Mi Account, ecran, baterie",
        "riscuri": "Cont blocat, confuzie F3/F5/X series",
        "sfat_revanzare": "Se vinde bine sub mediană în 2–5 zile",
    },
    "HUAWEI": {
        "segment": "Mid / lichiditate medie",
        "puncte_atentie": "Fără GMS pe modele noi, baterie, cont Huawei",
        "riscuri": "Absența serviciilor Google scade cererea",
        "sfat_revanzare": "Cumpără doar cu discount solid față de mediană",
    },
    "PIXEL": {
        "segment": "Nișă foto / lichiditate bună",
        "puncte_atentie": "baterie, ecran, FRP Google, update-uri",
        "riscuri": "FRP lock, stocare mică greu de revândut",
        "sfat_revanzare": "Modele recente curate se mișcă ok sub mediană",
    },
    "ONEPLUS": {
        "segment": "Mid-premium / cerere moderată",
        "puncte_atentie": "OxygenOS, baterie, ecran, cont",
        "riscuri": "Public mai mic decât Samsung/iPhone",
        "sfat_revanzare": "Necesită discount clar pentru flip rapid",
    },
    "MOTOROLA": {
        "segment": "Entry–mid",
        "puncte_atentie": "baterie, ecran, cont Google",
        "riscuri": "Marje mici pe OLX",
        "sfat_revanzare": "Doar la prețuri mult sub mediană",
    },
    "REALME": {
        "segment": "Mid-range",
        "puncte_atentie": "cont, baterie, stare",
        "riscuri": "Confuzie între serii GT/Narzo/number",
        "sfat_revanzare": "Flip ok cu discount 20%+",
    },
    "OPPO": {
        "segment": "Mid-range",
        "puncte_atentie": "cont, baterie, ecran",
        "riscuri": "Cerere moderată",
        "sfat_revanzare": "Caută sub mediană cu stare bună",
    },
    "VIVO": {
        "segment": "Mid-range / lichiditate medie",
        "puncte_atentie": "cont, baterie, ecran",
        "riscuri": "Se mișcă mai greu decât Samsung/Xiaomi",
        "sfat_revanzare": "Doar discount solid",
    },
    "NOTHING": {
        "segment": "Nișă",
        "puncte_atentie": "stare, cont, cutie",
        "riscuri": "Cerere limitată",
        "sfat_revanzare": "Cumpără doar foarte ieftin vs mediană",
    },
}

_PHONE_PATTERNS = [
    (r"\bIPHONE\b|\bI\s*PHONE\b", "IPHONE"),
    (r"\bSAMSUNG\b|\bGALAXY\b", "SAMSUNG"),
    (r"\bHONOR\b", "HONOR"),
    (r"\bXIAOMI\b|\bMI\s*\d", "XIAOMI"),
    (r"\bREDMI\b", "REDMI"),
    (r"\bPOCO\b", "POCO"),
    (r"\bHUAWEI\b", "HUAWEI"),
    (r"\bPIXEL\b", "PIXEL"),
    (r"\bONEPLUS\b|\bONE\s*PLUS\b", "ONEPLUS"),
    (r"\bMOTOROLA\b|\bMOTO\b", "MOTOROLA"),
    (r"\bREALME\b", "REALME"),
    (r"\bOPPO\b", "OPPO"),
    (r"\bVIVO\b", "VIVO"),
    (r"\bNOTHING\b", "NOTHING"),
]

GPUS_DATABASE = {
    "GT 710": {"scor": 640, "tdp": 19, "conector_alimentare": "Niciunul (Slot PCIe)", "vram": "1GB-2GB DDR3", "categorie": "Bază / Video"},
    "GT 730": {"scor": 820, "tdp": 38, "conector_alimentare": "Niciunul (Slot PCIe)", "vram": "2GB DDR3/GDDR5", "categorie": "Bază / Video"},
    "GT 1030": {"scor": 2600, "tdp": 30, "conector_alimentare": "Niciunul (Slot PCIe)", "vram": "2GB GDDR5", "categorie": "Entry-level / eSports Ușor"},
    "RX 550": {"scor": 2800, "tdp": 50, "conector_alimentare": "Niciunul (Slot PCIe)", "vram": "2GB-4GB GDDR5", "categorie": "Entry-level / eSports Ușor"},
    "GTX 750 TI": {"scor": 3900, "tdp": 60, "conector_alimentare": "Niciunul (Slot PCIe)", "vram": "2GB GDDR5", "categorie": "Entry-level Clasic"},
    "GTX 960": {"scor": 6050, "tdp": 120, "conector_alimentare": "1x 6 pini", "vram": "2GB-4GB GDDR5", "categorie": "Mid-range Vechi"},
    "GTX 1050": {"scor": 5300, "tdp": 75, "conector_alimentare": "Niciunul (Slot PCIe)", "vram": "2GB GDDR5", "categorie": "Entry-level Eficient"},
    "GTX 1050 TI": {"scor": 6400, "tdp": 75, "conector_alimentare": "Niciunul / 1x 6 pini", "vram": "4GB GDDR5", "categorie": "Entry-level Recomandat"},
    "RX 570": {"scor": 7000, "tdp": 150, "conector_alimentare": "1x 8 pini", "vram": "4GB-8GB GDDR5", "categorie": "Raport Calitate-Preț 1080p"},
    "RX 580": {"scor": 8800, "tdp": 185, "conector_alimentare": "1x 8 pini", "vram": "8GB GDDR5", "categorie": "Raport Calitate-Preț 1080p"},
    "GTX 1060 3GB": {"scor": 9600, "tdp": 120, "conector_alimentare": "1x 6 pini", "vram": "3GB GDDR5", "categorie": "Mid-range 1080p"},
    "GTX 1060 6GB": {"scor": 10200, "tdp": 120, "conector_alimentare": "1x 6 pini", "vram": "6GB GDDR5", "categorie": "Mid-range 1080p"},
    "GTX 1650": {"scor": 7800, "tdp": 75, "conector_alimentare": "Niciunul (Slot PCIe)", "vram": "4GB GDDR5/GDDR6", "categorie": "Entry-level Eficient"},
    "GTX 1660 SUPER": {"scor": 12700, "tdp": 125, "conector_alimentare": "1x 8 pini", "vram": "6GB GDDR6", "categorie": "Mid-range Puternic"},
    "RTX 2060": {"scor": 14100, "tdp": 160, "conector_alimentare": "1x 8 pini", "vram": "6GB-12GB GDDR6", "categorie": "Mid-range DLSS/RT"},
    "RTX 3060": {"scor": 17200, "tdp": 170, "conector_alimentare": "1x 8 pini", "vram": "12GB GDDR6", "categorie": "Mid-range Modern"},
    "RTX 4060": {"scor": 19700, "tdp": 115, "conector_alimentare": "1x 8 pini", "vram": "8GB GDDR6", "categorie": "Generația Actuală Eficientă"},
    "RTX 5070": {"scor": 28000, "tdp": 220, "conector_alimentare": "16 pini / 2x8 pini", "vram": "12GB GDDR7", "categorie": "High-End Generația Următoare"},
}

CONSOLES_DATABASE = {
    "PS VITA": {
        "tip": "Portabil",
        "ecran": "OLED 5' (model 1000) sau LCD (model 2000 Slim)",
        "ecosistem": "Deblocare ușoară (Henkaku/Enso)",
        "emulare": "PS1, PSP, retro",
        "puncte_atentie": "Drift stick, burn-in OLED",
    },
    "NINTENDO 3DS": {
        "tip": "Portabil",
        "ecran": "Dual Screen 3D",
        "ecosistem": "CFW Luma3DS",
        "emulare": "DS/GBA",
        "puncte_atentie": "Ecrane, balama, touch",
    },
    "NINTENDO 2DS": {
        "tip": "Portabil",
        "ecran": "Dual Screen 2D",
        "ecosistem": "Compatibil 3DS/DS",
        "emulare": "DS/GBA",
        "puncte_atentie": "Ecrane și butoane L/R",
    },
    "PSP": {
        "tip": "Portabil",
        "ecran": "LCD 4.3'",
        "ecosistem": "CFW PRO/Infinity",
        "emulare": "PS1 / retro",
        "puncte_atentie": "Baterie umflată",
    },
    "NINTENDO SWITCH": {
        "tip": "Hibrid",
        "ecran": "LCD/OLED",
        "ecosistem": "Bibliotecă actuală; V1 eventual RCM",
        "emulare": "Nativă",
        "puncte_atentie": "Joy-Con drift, ban NNID",
    },
}


def identifica_hardware(titlu):
    """Identifică telefon (prioritar), apoi consolă sau GPU din titlu."""
    t = titlu.upper()

    for pattern, brand in _PHONE_PATTERNS:
        if re.search(pattern, t):
            return brand, "telefon", TELEFOANE_DATABASE[brand]

    if "VITA" in t or "PSVITA" in t:
        return "PS VITA", "console", CONSOLES_DATABASE["PS VITA"]
    if "3DS" in t:
        return "NINTENDO 3DS", "console", CONSOLES_DATABASE["NINTENDO 3DS"]
    if "2DS" in t:
        return "NINTENDO 2DS", "console", CONSOLES_DATABASE["NINTENDO 2DS"]
    if re.search(r"\bPSP\b", t):
        return "PSP", "console", CONSOLES_DATABASE["PSP"]
    if "SWITCH" in t:
        return "NINTENDO SWITCH", "console", CONSOLES_DATABASE["NINTENDO SWITCH"]

    gpus_sortate = sorted(GPUS_DATABASE.keys(), key=lambda x: len(x), reverse=True)
    for gpu_name in gpus_sortate:
        sablon = r"\b" + re.escape(gpu_name) + r"\b"
        if re.search(sablon, t):
            return gpu_name, "gpu", GPUS_DATABASE[gpu_name]

    return None, None, None


def calculeaza_cost_pe_punct(pret, scor_g3d):
    if not scor_g3d or scor_g3d <= 0 or pret <= 0:
        return 0.0
    return round(pret / scor_g3d, 3)
