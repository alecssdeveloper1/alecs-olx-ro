# Audit Alecs OLX + Vinted

Data: 2026-11-25. Proiect: `/root/projects/apps/alecs-olx-ro`. Proces live: `alecs-olx.service` → `monitor.py`. Nu există `main.py`.

Scop: bot Telegram de arbitraj telefoane. Cumpără sub piață pe OLX sau Vinted, revinde pe OLX. Evaluare Gemini, mediană din SQLite, alertă dacă scorul IA ≥ 6 și marja netă ≥ `MIN_MARJA_LEI` (implicit 200), după `COST_FLIP_LEI` (implicit 25).

## Fișiere

| Fișier | Rol |
|---|---|
| `monitor.py` (~1500 rânduri) | Telegram, SQLite, bucla OLX, bucla Vinted, prompt Gemini, mesajul de alertă |
| `olx_ro.py` | URL OLX, parsare linkuri, model exact, stocare, stare, variantă, încredere |
| `vinted_ro.py` | Catalog HTML Vinted, `fetch_item`, cost de achiziție, `classify_listings` / `select_deals` |
| `market_analytics.py` | Mediană de revânzare doar din anunțuri OLX, lichiditate, termeni de risc |
| `benchmarks.py` | Profil hardware din titlu (telefon / GPU / consolă) |
| `tests/` | 20 teste unittest. Nu acoperă bucla din `monitor.py` |
| `.env` | `TELEGRAM_TOKEN`, `GEMINI_API_KEY`, interval, praguri. Nu e în git ca secret, există `.env.example` |

Dependențe folosite: `curl_cffi`, `beautifulsoup4`, `google-genai`, `requests`, `python-dotenv`. `python-telegram-bot` e în `requirements.txt` dar nu e folosit. Telegram e HTTP direct (`getUpdates`, `sendMessage`, `sendPhoto`).

## Flux

Un proces, două fire.

1. Firul Telegram citește comenzile (`/add`, `vinted …`, listă, ștergere, limbaj natural prin Gemini).
2. Firul principal, la fiecare `INTERVALO_SEGUNDOS` (implicit 45s): `monitorar_rodada()` apoi `monitorar_vinted()`.
3. OLX: la fiecare rundă pagina 1, sortare cele mai noi. La fiecare a 10-a rundă, până la 25 de pagini.
4. Vinted: doar pagina 1, `order=newest_first`, pe `https://www.vinted.ro`.

OLX, alertă:
- descarcă anunțul (`fetch_item` → JSON-LD);
- verifică modelul exact, accesoriile/piesele/lock, prețul față de plafon;
- `inspect_listing` (stocare, stare, variantă; respinge dacă titlul și descrierea au GB diferite);
- Gemini + mediana OLX;
- trimite doar dacă scor ≥ 6, există ≥ 3 anunțuri OLX comparabile și marja netă ≥ prag.

Vinted, alertă (arbitraj Vinted → OLX):
- `classify_listings` / `select_deals`: mediana pe toate anunțurile comparabile de pe pagină, apoi plafonul utilizatorului;
- intră la Gemini doar dacă costul (preț + protecția cumpărătorului, dacă există) e ≤ plafon și cu ≥ 20% sub mediana Vinted, și sunt ≥ 4 comparabile (`MIN_SAMPLE`);
- sub 45% din mediană nu e șters: e `suspect` și descrierea e verificată;
- marja = revânzare rapidă OLX − costul Vinted − 25 lei;
- fără ≥ 3 anunțuri OLX nu se trimite alertă de profit;
- maxim 4 apeluri Gemini per căutare per rundă.

## Date

SQLite `ofertas_ro.db`.

- `ads`: cache anunțuri. Coloană `source` (`olx` / `vinted`). Rândurile `id LIKE 'vinted:%'` sunt marcate `vinted`.
- `searches`: căutări per `chat_id`, cu `source`.
- `sent_notifications`: `(ad_id, chat_id)` plus `status` (`sent` / `rejected` / `watch`) și `seen_price`.
- `market_analytics.obtine_statistici_piata` citește doar `source = olx` și ignoră `id LIKE 'vinted:%'`. Dacă coloana `source` lipsește, filtrează doar după id.

Vinted, stări la revedere:
- `rejected`: model greșit, accesoriu, piesă/lock, descriere care contrazice stocarea. Nu se mai redeschide.
- `watch`: peste buget, sub pragul de 20%, sau evaluat dar fără marjă. Se redeschide doar dacă prețul scade cu peste ~0,50 lei.
- `sent`: alerta a plecat.

OLX încă marchează tot ce a procesat, inclusiv „preț peste plafon”, ca notificat definitiv. O reducere ulterioară pe OLX nu se reanalizează.

## Reguli de business care sunt în cod

- Model exact: tokenii căutării trebuie să fie în titlu. Variantele din anunț (Pro, Lite, Ultra, Max, …) trebuie să fie subset al căutării. Dacă în căutare e o stocare, anunțul cu altă stocare e respins.
- Accesorii Vinted: listă de tokeni (husă, maska, kryt, etui, …). Dacă tokenul e în căutare, nu se filtrează.
- Stocare: 32 / 64 / 128 / 256 / 512 / 1024. O singură valoare clară. Cohortă de preț pe aceeași stocare doar dacă există ≥ 3 titluri cu acea stocare; altfel mediana rămâne pe toată varianta.
- Stare: `sigilat` / `nou` / `utilizat` / `necunoscut`, din text sau din eticheta Vinted (`New with tags` → sigilat).
- Încrederea (0–100) e afișată. Nu blochează alerta.
- Sub 3 prețuri OLX: `esantion_suficient = false`, `marja_profit = 0`, `discount_pct = 0`. Nu se mai folosește `preț * 1.25`.
- Scorul Gemini e tăiat de `align_score_with_market`: sub 20% discount sau sub 3 eșantioane, maxim 6; peste mediană, maxim 4.
- Transportul Vinted nu e în pagină. Nu e în marjă. Intră doar protecția cumpărătorului (`total_price`) și cei 25 lei.

## Ce s-a reparat deja (nu mai e de redeschis ca bug deschis)

- `monitorar_vinted` apelează `classify_listings` (iar `select_deals` e wrapper peste ea).
- Mediana Vinted nu se mai calculează doar din anunțurile de sub plafon.
- Chilipirul foarte ieftin rămâne, cu flag `suspect`.
- Costul trimis la Gemini și la marjă este `acquisition_cost`, nu prețul gol, când Vinted arată totalul cu protecție.
- Prețurile Vinted nu mai intră în mediana de revânzare OLX.
- Nu se mai trimite profit „confirmat” din mediana inventată `preț * 1.25`.

## Probleme rămase

1. ~~OLX fără watch la reduceri~~ → **reparat P1**: preț peste plafon / marjă insuficientă → `watch`; redeschidere la scădere sau rundă deep.
2. ~~Vinted watch doar la scădere de preț~~ → **reparat P1**: `should_reopen_watch(..., force_reeval=is_deep_round())` pentru date OLX noi.
3. ~~Mediană Vinted mixtă~~ → **reparat P1**: cohortă omogenă (protecție dacă ≥ `MIN_SAMPLE` și majoritate; altfel doar listat).
4. Mai multe anunțuri foarte ieftine (neprinși de filtrul de accesorii) intră în mediană. Mediana e robustă la un singur outlier, nu la un grup.
5. `MIN_SAMPLE = 4`. Trei anunțuri bune pe pagină nu produc nicio ofertă Vinted.
6. ~~Transport lipsă~~ → **reparat P1**: `VINTED_SHIPPING_LEI` (implicit 20) în `acquisition_cost`.
7. ~~SQLite fără lock~~ → **reparat P0**: `db_session()` + `threading.RLock`, WAL, `busy_timeout=30s`.
8. `OLX_ACCESS_BLOCKED` rămâne true până la restart. Vinted continuă.
9. ~~Fără allowlist Telegram~~ → **reparat P0**: `TELEGRAM_ALLOWED_CHAT_IDS` (fail-closed dacă e gol).
10. ~~Top/Digest fără filtru source~~ → **reparat P2**: doar `source=olx` / fără `vinted:%`.
11. ~~`created_at` resetat~~ → **reparat P2**: `ON CONFLICT DO UPDATE` fără a atinge `created_at`.
12. Încrederea și starea nu intră în pragul de alertă. Un „necunoscut” cu scor 6 tot pleacă, dacă marja trece.
13. Parsarea Vinted depinde de `data-testid` și de titlul din `a[title]` (înainte de `, Brand:`). O schimbare de HTML rupe catalogul fără test de contract live.
14. Gemini: lista de modele e fixă în cod (`gemini-3.1-flash-lite`, `gemini-3.5-flash-lite`, `gemini-3.5-flash`). Promptul cere JSON. Eșecul de parse sare la modelul următor, apoi renunță la anunțul ăla (Vinted îl reia; OLX pe eroare AI poate rămâne nemarcat dacă excepția iese din bucla interioară).
15. ~~Fără teste pe stările buclelor~~ → **parțial P2**: `tests/test_watch_loops.py` (watch, deep, source, created_at). Nu rulează scrape live.

## Cum să verifici un patch

- `cd /root/projects/apps/alecs-olx-ro && .venv/bin/python -m unittest tests.test_olx_ro tests.test_vinted_ro tests.test_phones -q`
- Nu reporni serviciul decât dacă se schimbă codul care trebuie să fie live: `systemctl restart alecs-olx.service`.
- Nu pune tokenul sau cheia Gemini în diff.
- Nu amesteca iar `ads` Vinted în `obtine_statistici_piata`.
- Păstrează poarta `esantion_suficient` înainte de orice alertă de profit.
