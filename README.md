# 🏆 FIFA World Cup 2026 — Calendario y Resultados

App ligera que:

1. **Consulta automáticamente cada 4 horas** desde la nube (GitHub
   Actions), incluso si tu PC está apagado: calendario, marcadores,
   **cuotas 1X2**, **stats de los últimos 10 partidos por equipo**
   (goles, tarjetas, expulsiones) y **lesiones**.
2. Guarda todo en archivos JSON versionados en `data/`.
3. Visor web (modo claro/oscuro) con 5 pestañas: Calendario, Posiciones,
   **Cuotas**, **Stats equipos**, **Lesiones**.
4. **Validación batch en cada corrida**: si algo se ve raro (cuota
   con drift > 50%, fechas fuera del Mundial, fuente caída) abre un
   Issue de GitHub con el detalle.
5. Opcionalmente, mensaje por Telegram cuando termina un partido.

## Arquitectura

```
┌──────────────────────┐    cron cada 4h
│  GitHub Actions      │ ──┬─► football-data.org   (matches + bookings WC)
│  (workflow .yml)     │   ├─► The-Odds-API / ESPN / bayesian mirror (cuotas)
└──────────┬───────────┘   ├─► raw bayesian results.csv (form últimos 10)
           │ git commit    ├─► API-Football            (lesiones)
           │               └─► Telegram Bot API        (notificaciones)
           ▼
┌──────────────────────┐                     ┌──────────────────────┐
│  data/*.json         │ ◄────fetch()─────── │  viewer/index.html   │
│  (en tu repo)        │                     │  (abierto en local)  │
└──────────────────────┘                     └──────────────────────┘
```

## Estructura

```
worldcup_calendar/
├── .github/workflows/update-results.yml   # Cron cada 4h en la nube
├── scripts/
│   ├── fetch_matches.py                   # WC fixtures + scores
│   ├── fetch_odds.py                      # Cuotas 1X2 (3 fuentes con fallback)
│   ├── fetch_team_stats.py                # Forma últimos 10 + tarjetas WC
│   ├── fetch_injuries.py                  # Lesiones (API-Football)
│   ├── validate_data.py                   # QA + abre Issue si hay anomalías
│   └── notify_telegram.py                 # Notifica partidos finalizados
├── data/
│   ├── matches.json
│   ├── odds.json
│   ├── team_stats.json
│   ├── injuries.json
│   └── validation_report.json             # último resultado del validador
├── viewer/index.html                      # Visor web (vanilla JS)
├── setup.ps1                              # Setup automatizado (gh CLI)
├── requirements.txt
└── README.md
```

## Fuentes de datos y cómo configurarlas

| Dato | Script | Fuente principal | Fallback | Secret/Var requerido |
|---|---|---|---|---|
| Calendario, marcadores | `fetch_matches.py` | football-data.org `/competitions/WC/matches` | — | `FOOTBALL_DATA_API_KEY` |
| Cuotas 1X2 | `fetch_odds.py` | The-Odds-API (multi-bookie, free 500/mes) | ESPN scraping → CSV mirror del bayesiano | `THE_ODDS_API_KEY` (opc.), `BAYESIAN_ODDS_URL` (opc.) |
| Forma últimos 10 (goles) | `fetch_team_stats.py` | mirror `results.csv` del bayesiano | — | `BAYESIAN_RESULTS_URL` (var) |
| Tarjetas/expulsiones (durante WC) | `fetch_team_stats.py` | football-data.org `/matches/{id}` bookings | — | `FOOTBALL_DATA_API_KEY` |
| Lesiones | `fetch_injuries.py` | API-Football v3 (free 100/día) | Wikipedia (best-effort) | `API_FOOTBALL_KEY` (opc.) |
| Notificaciones | `notify_telegram.py` | Telegram Bot API | — | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` |

> **Variables vs Secrets en GitHub.** Las URLs públicas (raw del repo
> bayesiano, etc.) van como **Variables** del repo (`Settings → Secrets
> and variables → Actions → Variables`). Las API keys siempre como
> **Secrets**.



## Setup (una sola vez, ~10 minutos)

> 💡 **Atajo:** si tienes [GitHub CLI](https://cli.github.com/) instalado y
> autenticado (`gh auth login`), ejecuta:
>
> ```powershell
> .\setup.ps1 -RepoName worldcup-2026
> ```
>
> El script crea el repo, sube el código, configura los secrets y dispara
> la primera ejecución. Te imprime al final la URL que debes pegar en el
> visor. Salta directo al paso **5** si lo usas.

### 1. Obtén una API key gratis de football-data.org

1. Entra a <https://www.football-data.org/client/register> y regístrate
   (solo email, sin tarjeta).
2. Copia el **API Token** que te envían por correo. La cuenta free permite
   10 peticiones/minuto — más que suficiente para 6 ejecuciones al día.

### 2. Crea un repositorio en GitHub

```powershell
cd "c:\Users\J9B3M7E8\OneDrive - fundaciongruposocial.co\Compu viejo y desgastado\05_Proyectos-Personales\worldcup_calendar"
git init
git add .
git commit -m "feat: initial WC2026 calendar app"
git branch -M main
# Crea el repo en https://github.com/new (público o privado, da igual)
git remote add origin https://github.com/<tu-usuario>/<tu-repo>.git
git push -u origin main
```

### 3. Agrega la API key como Secret del repo

En GitHub: **Settings → Secrets and variables → Actions → New repository secret**

- Name: `FOOTBALL_DATA_API_KEY`
- Value: *(el token que obtuviste en el paso 1)*

### 3b. (Opcional) Configura notificaciones por Telegram

Si quieres recibir un mensaje cada vez que termine un partido:

1. En Telegram, busca `@BotFather`, envía `/newbot`, sigue las instrucciones
   y guarda el **bot token** que te entrega (formato `123456:ABC-DEF...`).
2. Abre un chat con tu nuevo bot y envíale cualquier mensaje (por ejemplo `hola`).
3. Visita `https://api.telegram.org/bot<TU_TOKEN>/getUpdates` en el navegador
   y copia el `"chat":{"id": ...}` que aparece (es tu `chat_id`).
4. Agrega dos secrets más al repo:
   - `TELEGRAM_BOT_TOKEN` = el token del paso 1
   - `TELEGRAM_CHAT_ID`   = el id del paso 3

> El workflow detecta automáticamente si los secrets existen. Si están
> vacíos, simplemente no envía nada.

### 4. Dispara la primera ejecución manualmente

En GitHub: **Actions → Update World Cup 2026 results → Run workflow**.

A los ~30 segundos verás un commit nuevo: `chore(data): refresh WC2026 matches`.

### 5. Configura el visor

Edita [viewer/index.html](viewer/index.html) y cambia la constante
`BASE_URL` para apuntar a la **carpeta** `data/` (no a un archivo):

```js
const BASE_URL = "https://raw.githubusercontent.com/<tu-usuario>/<tu-repo>/main/data";
```

El visor lee de allí `matches.json`, `odds.json`, `team_stats.json` e
`injuries.json`. Si alguno no existe todavía, esa pestaña simplemente
muestra estado vacío.

> Para repos **privados**, el raw URL requiere autenticación. Si tu repo
> es privado y quieres mantenerlo simple, hazlo público (los datos son
> resultados deportivos, no hay nada sensible) o publica el JSON con
> GitHub Pages.

Haz commit y push del cambio. Listo.

### 5b. (Opcional) Activa fuentes adicionales

Todas son opcionales. El workflow corre con las que tengas configuradas
y reporta las que faltan en el `validation_report.json`.

| Para activar… | Tipo | Nombre | Cómo obtenerlo |
|---|---|---|---|
| Cuotas multi-bookie premium | Secret | `THE_ODDS_API_KEY` | <https://the-odds-api.com> (free 500 req/mes) |
| Mirror de cuotas del bayesiano | Variable | `BAYESIAN_ODDS_URL` | URL raw de `bayesian_model/data/market_odds.csv` |
| Forma últimos 10 (goles) | Variable | `BAYESIAN_RESULTS_URL` | URL raw de `bayesian_model/data/results.csv` |
| Lesiones | Secret | `API_FOOTBALL_KEY` | <https://www.api-football.com> (free 100/día) |
| Fallback Wikipedia para lesiones | Variable | `WIKI_INJURIES_FALLBACK=1` | — |

Configurar en GitHub:
**Settings → Secrets and variables → Actions →** pestaña **Secrets** o **Variables**.

### 6. Uso diario

Abre `viewer/index.html` con doble clic en el explorador de Windows. El
visor lee el JSON desde el raw de GitHub. Cada 4 h el cron actualiza el
archivo y al recargar (botón **↻ Recargar**) ves los nuevos resultados.

## Pruebas locales (opcional)

Si quieres probar el fetch en tu máquina antes de subirlo:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:FOOTBALL_DATA_API_KEY = "tu_token_aqui"
python scripts/fetch_matches.py
```

Para visualizar el JSON local, abre el visor con un servidor estático
(las restricciones de CORS de `file://` impiden cargarlo directamente):

```powershell
python -m http.server 8000
# Luego abre: http://localhost:8000/viewer/
```

## Personalización

| Quiero…                                  | Edita…                                                     |
|------------------------------------------|------------------------------------------------------------|
| Cambiar la frecuencia del cron           | `cron:` en [.github/workflows/update-results.yml](.github/workflows/update-results.yml#L6) |
| Cambiar los campos guardados             | `normalize()` en [scripts/fetch_matches.py](scripts/fetch_matches.py#L31) |
| Cambiar colores/layout del visor         | bloque `<style>` en [viewer/index.html](viewer/index.html#L8) |
| Cambiar formato del mensaje de Telegram  | `format_message()` en [scripts/notify_telegram.py](scripts/notify_telegram.py#L34) |
| Agregar notificación por email/Discord   | Nuevo step en el workflow tras el fetch                    |

## Limitaciones conocidas

- **football-data.org tier free**: 10 req/min, 10 competiciones. La
  competición `WC` está incluida. Si el Mundial coincide con otros
  fixtures del mismo plan, podrías toparte con el rate limit; el script
  solo hace 1 request, así que es seguro.
- **GitHub Actions cron**: la ejecución puede retrasarse algunos minutos
  bajo alta carga de la plataforma; el "cada 4 h" es aproximado.
- **Datos en vivo**: la API actualiza marcadores con algunos minutos de
  retraso respecto al partido real. Para "tiempo real" exacto necesitarías
  una API de pago.

## Licencia

Uso personal. Los datos de partidos son propiedad de football-data.org —
revisa sus [términos](https://www.football-data.org/terms).
