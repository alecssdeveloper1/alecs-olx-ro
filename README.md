# Alecs OLX RO

Bot Telegram pentru **arbitraj pe telefoane** pe OLX România: găsește anunțuri sub prețul pieței pe **model exact**, le evaluează cu Gemini și te anunță pe Telegram.

Repo privat: [alecssdeveloper1/alecs-olx-ro](https://github.com/alecssdeveloper1/alecs-olx-ro)

---

## Ce face

1. Monitorizează căutările tale pe **OLX.ro** (toată țara, fără filtru județ).
2. Păstrează doar **modelul exact** (`honor 600` ≠ Smart/Pro; `iphone 12` ≠ 12 Pro).
3. Compară prețul cu **mediana** din istoricul local.
4. Evaluează oferta cu **Google Gemini** (scor 0–10, tier S/A/B/C).
5. Trimite alertă pe Telegram dacă scorul e ≥ 6.

Optimizat pentru telefoane (iPhone, Samsung, Honor, Xiaomi etc.). GPU/console rămân suportate secundar.

---

## Structură

```text
alecs-olx-ro/
├── monitor.py           # Daemon: scraping OLX + Telegram + AI
├── olx_ro.py            # URL-uri OLX.ro, prețuri lei, potrivire model exact
├── market_analytics.py  # Mediană, discount, marjă, lichiditate telefoane
├── benchmarks.py        # Checklist flip telefoane (+ GPU/console)
├── tests/               # Teste unitare
├── requirements.txt
├── .env.example
└── ofertas_ro.db        # SQLite (se creează automat, nu e în git)
```

---

## Instalare (PC sau VPS)

### Cerințe

- Python 3.10+
- Token Telegram de la [@BotFather](https://t.me/BotFather)
- Cheie Gemini de la [Google AI Studio](https://aistudio.google.com/)

### Pași

```bash
git clone https://github.com/alecssdeveloper1/alecs-olx-ro.git
cd alecs-olx-ro

python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

pip install -r requirements.txt

cp .env.example .env
```

Editează `.env`:

```env
TELEGRAM_TOKEN="tokenul_tau_telegram"
GEMINI_API_KEY="cheia_ta_gemini"
INTERVALO_SEGUNDOS=90
```

### Pornire manuală

```bash
python -u monitor.py
```

Apoi în Telegram: deschide botul → `/start`.

---

## Instalare pe VPS (24/7 cu systemd)

```bash
# după clone + venv + .env (vezi mai sus)
sudo nano /etc/systemd/system/alecs-olx.service
```

Conținut (înlocuiește `USER` și path-ul):

```ini
[Unit]
Description=Alecs OLX RO Bot
After=network.target

[Service]
Type=simple
User=USER
WorkingDirectory=/home/USER/alecs-olx-ro
Environment=PYTHONUNBUFFERED=1
ExecStart=/home/USER/alecs-olx-ro/.venv/bin/python -u /home/USER/alecs-olx-ro/monitor.py
Restart=always
RestartSec=15

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now alecs-olx.service
sudo systemctl status alecs-olx.service
journalctl -u alecs-olx.service -f
```

---

## Cum se folosește (Telegram)

Trimite `/start` — apar butoanele:

| Buton | Ce face |
| :--- | :--- |
| **➕ Adaugă** | Apoi scrii modelul + preț max, ex: `honor 600 1500` |
| **📋 Listă** | Căutările active |
| **🗑 Șterge** | Alegi ce elimini (buton pe fiecare) |
| **🏆 Top** | Cele mai bune oferte globale (scor 8–10) |
| **📊 Tierlist** | Top pe căutările tale |
| **❓ Ajutor** | Rezumat comenzi |

### Exemple de adăugare

```text
honor 600 1500
iphone 12 1200
samsung s23 1800
honor 600 pro 2500
```

Poți folosi și comenzi: `/add`, `/list`, `/del`, `/top`, `/tierlist`  
sau limbaj natural: *„Caut iPhone 13 până la 2.000 lei”*.

### Reguli importante

- Scrie **modelul exact** pe care vrei să-l cumperi pentru revânzare.
- Botul respinge variantele (Pro/Smart/Lite/Max) dacă nu le-ai cerut.
- La telefoane ignoră accesorii/piese (husă, folie, display etc.).
- Alerta vine doar dacă AI dă scor ≥ 6 și prețul e sub plafonul tău.
- Scanare: ~la 90s, max 5 pagini × 15 anunțuri per căutare.

---

## Disclaimer

Automatizarea pe OLX poate fi restricționată de termenii platformei. Folosește responsabil, pe cont propriu.  
Lichiditatea și timpul de vânzare sunt **euristici**, nu garanții.  
La HTTP 403/429 botul oprește scanarea până la restart (nu încearcă ocoliri).

Bazat pe proiectul upstream [garimpo-olx-bot](https://github.com/Guilherme-L-C/garimpo-olx-bot) (MIT), adaptat pentru OLX România și telefoane.

---

## Contact

- **WhatsApp:** [+40 732 159 658](https://wa.me/40732159658)
- **Telegram:** [@alecss12](https://t.me/alecss12)
