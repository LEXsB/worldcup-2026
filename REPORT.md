# 📋 Reporte de proyecto — `worldcup-2026`

> **Estado:** En producción · **Repo:** <https://github.com/LEXsB/worldcup-2026>
> **Última actualización del reporte:** 16 de junio de 2026

---

## 1. Resumen ejecutivo

App ligera **100% serverless en GitHub** que ofrece:

- Calendario y resultados en vivo del **Mundial 2026** (104 partidos).
- **Cuotas 1X2** de varias casas de apuestas (multi-bookmaker).
- **Forma reciente** (últimos 10 partidos) de las 48 selecciones: GF, GC, W-D-L.
- **Tarjetas amarillas / rojas / segunda amarilla** acumuladas durante el Mundial.
- **Lesiones** por equipo (API-Football + Wikipedia).
- **Tabla de posiciones por grupo** calculada en vivo.
- **Canales de TV** que transmiten cada partido en Colombia (jerarquía Caracol → RCN → Win+ → Disney+ → Paramount+ → DSports).
- **Validación batch** automática que abre Issues de GitHub si detecta anomalías (drift de cuotas > 50 %, fechas fuera de rango, fuentes caídas).
- Notificaciones opcionales por **Telegram** cuando termina un partido.

El cron corre **cada 4 horas en los servidores de GitHub Actions** — el PC del usuario nunca tiene que estar encendido y nunca contacta sitios bloqueados (apuestas, deportes, etc.). El visor local solo lee JSON desde `raw.githubusercontent.com`, que sí está permitido en el firewall corporativo.

---

## 2. Arquitectura

```
┌──────────────────────┐    cron cada 4h
│  GitHub Actions      │ ──┬─► football-data.org   (matches + bookings WC)
│  (workflow .yml)     │   ├─► The-Odds-API / ESPN / mirror bayesiano (cuotas)
└──────────┬───────────┘   ├─► raw martj42 results.csv (form últimos 10)
           │ git commit    ├─► API-Football (lesiones) + Wikipedia (fallback)
           │               └─► Telegram Bot API (notificaciones)
           ▼
┌──────────────────────┐                     ┌──────────────────────┐
│  data/*.json         │ ◄────fetch()─────── │  viewer/index.html   │
│  (en tu repo)        │                     │  (abierto en local)  │
└──────────────────────┘                     └──────────────────────┘
```

---

## 3. Estructura del repositorio

```
worldcup-2026/
├── .github/workflows/update-results.yml   # Cron cada 4h en la nube
├── scripts/
│   ├── fetch_matches.py                   # WC fixtures + scores
│   ├── fetch_odds.py                      # Cuotas 1X2 (3 fuentes con fallback)
│   ├── fetch_team_stats.py                # Forma últimos 10 + tarjetas WC
│   ├── fetch_injuries.py                  # Lesiones (API-Football + Wikipedia)
│   ├── validate_data.py                   # QA + abre Issue si hay anomalías
│   └── notify_telegram.py                 # Notifica partidos finalizados
├── data/
│   ├── matches.json                       # Calendario y marcadores
│   ├── odds.json                          # Cuotas 1X2
│   ├── team_stats.json                    # Forma + tarjetas/expulsiones
│   ├── injuries.json                      # Lesiones
│   ├── validation_report.json             # Último reporte de QA
│   ├── team_aliases.json                  # Mapa de alias canónicos
│   └── broadcasts_co.json                 # Canales TV en Colombia
├── viewer/index.html                      # Visor web (vanilla JS)
├── setup.ps1                              # Setup automatizado (gh CLI)
├── REPORT.md                              # Este reporte
├── README.md
└── requirements.txt
```

---

## 4. Fuentes de datos y estado

| Dato | Fuente principal | Fallback | Estado actual | Cuota free |
|---|---|---|---|---|
| Calendario WC | football-data.org `/competitions/WC/matches` | — | ✅ 104 partidos | 10 req/min |
| Marcadores | football-data.org (mismo endpoint) | — | ✅ Auto cada 4 h | — |
| Cuotas 1X2 | **The-Odds-API** | ESPN scraping → mirror bayesiano | ✅ ~56 partidos cubiertos | 500 req/mes |
| Tarjetas/rojas WC | football-data.org `/matches/{id}` bookings | — | ✅ 1 fila por equipo + acumulado | comparte cuota matches |
| Forma últimos 10 | mirror raw `martj42/international_results/results.csv` | — | ✅ 48/48 equipos con datos | sin límite |
| Lesiones | **API-Football** `/injuries?league=1&season=2026` | Wikipedia ES/EN | ⚠️ Iteración en curso (re-corriendo workflow) | 100 req/día |
| Canales TV (CO) | Manual (`data/broadcasts_co.json`) | DGO+Paramount+ default | ✅ 72/72 partidos fase grupos | — |

---

## 5. Visor web (viewer/index.html)

Cinco pestañas, vanilla JS sin dependencias, modo claro/oscuro persistente en `localStorage`:

| Pestaña | Contenido |
|---|---|
| 📅 **Calendario** | Tabla con fecha en hora **Colombia (UTC-5)**, etapa, grupo, equipos, marcador, estado, sede y **canales TV en jerarquía**. Filtros: equipo, etapa, grupo, estado, fechas desde/hasta, **botón "Hoy"**. |
| 📊 **Posiciones** | Calculadas en vivo desde los partidos finalizados. Pts → DG → GF. Top 2 resaltados. |
| 💰 **Cuotas** | 1X2 multi-bookmaker con probabilidades sin vig. |
| 🔢 **Stats equipos** | Forma últimos 10: PJ, W-D-L con badges visuales, GF/p, GC/p, últimos 5 resultados como pildoras, 🟨 totales y promedio del WC, 🟥 totales. |
| 🩼 **Lesiones** | Grid por equipo con jugadores, estado y motivo. |

**Bug fixes recientes:**
- Filtro de fecha y columna "Fecha" forzados a hora Colombia (UTC-5) — antes el filtro "16-jun a 16-jun" mostraba un partido del 15-jun por desfase con UTC.
- Aliases de equipos para que Czechia, Cape Verde Islands, Congo DR, Bosnia-Herzegovina, etc., se empaten entre football-data.org y martj42.

---

## 6. GitHub Actions: workflow `update-results.yml`

Cron `0 */4 * * *` (cada 4 horas en UTC). Pasos:

1. Snapshot del estado anterior de los 4 JSON (para diff y validación).
2. `fetch_matches.py` — calendario + marcadores.
3. `fetch_odds.py` — cuotas 1X2 con fallback de 3 fuentes.
4. `fetch_team_stats.py` — forma últimos 10 + tarjetas WC.
5. `fetch_injuries.py` — lesiones (API-Football + Wikipedia complementario).
6. `validate_data.py` — QA y abre Issue si hay anomalías graves.
7. `notify_telegram.py` — opcional, si hay partidos recién finalizados.
8. `git commit` + push si hay cambios reales.

**Cada step tiene `continue-on-error: true`** — un fallo en una fuente no tumba el resto del pipeline.

---

## 7. Configuración (Secrets y Variables del repo)

### Secrets (sensibles)

| Nombre | Origen | Estado | Cuota |
|---|---|---|---|
| `FOOTBALL_DATA_API_KEY` | <https://www.football-data.org/client/register> | ✅ configurado | 10 req/min |
| `THE_ODDS_API_KEY` | <https://the-odds-api.com> | ✅ configurado | 500 req/mes |
| `API_FOOTBALL_KEY` | <https://www.api-football.com> | ✅ configurado | 100 req/día |
| `TELEGRAM_BOT_TOKEN` | @BotFather | ⏸️ no configurado (opcional) | — |
| `TELEGRAM_CHAT_ID` | `getUpdates` API | ⏸️ no configurado (opcional) | — |

> ⚠️ **Pendiente del usuario:** rotar los 3 tokens activos (los pegó en el chat). Cada proveedor tiene un botón "Regenerate" en su dashboard.

### Variables (no sensibles)

| Nombre | Valor actual |
|---|---|
| `BAYESIAN_RESULTS_URL` | `https://raw.githubusercontent.com/martj42/international_results/master/results.csv` |
| `WIKI_INJURIES_FALLBACK` | `1` (activo) |

---

## 8. Validación batch (`validate_data.py`)

Compara cada corrida vs la anterior:

- Conteo de partidos > 0
- Fechas dentro de `2026-06-01..2026-07-31`
- Cobertura de cuotas
- Drift de cuotas individuales > 50 % entre runs
- Fuentes que reportaron error
- Forma reciente: cuántos equipos sin datos
- Lesiones: cuántos equipos cubiertos

Si hay anomalías de nivel `warn`/`error`, abre **Issue de GitHub** con etiqueta `data-anomaly` y un fingerprint corto para evitar duplicados.

> **Issue abierto actualmente:** [#1](https://github.com/LEXsB/worldcup-2026/issues/1) (lesiones sin datos al inicio — ya corregido en la última iteración).

---

## 9. Cronología de cambios (commits clave)

| Commit | Descripción |
|---|---|
| `9656a76` | feat: WC2026 calendar app initial — estructura, fetcher de matches, visor con 5 pestañas |
| `15961df` | chore(data): primera corrida exitosa — 104 partidos, 56 cuotas, 48 equipos con forma |
| `e731023` | feat(viewer): TV channels per match (Colombia), filtro CO, botón "Hoy", aliases |
| `0daa395` | chore(data): refresh con aliases aplicados — Czechia, Cape Verde Islands, Congo DR ya con stats |
| `886e209` | fix(viewer): forzar hora Colombia en columna Fecha para coincidir con grilla TV |
| `2e3a3c2` | fix: rewrite fetch_injuries — 1 sola call (`league=1&season=2026`) + Wikipedia complementario |

---

## 10. Limitaciones conocidas

| Limitación | Causa | Mitigación |
|---|---|---|
| Cuotas no cubren todos los 104 partidos | Tier free de The-Odds-API solo expone juegos próximos | El fallback ESPN suele cubrir lo faltante; si no, simplemente no se muestran cuotas |
| Lesiones pueden tener cobertura parcial | API-Football tracking de selecciones nacionales es más débil que el de clubes | Wikipedia rellena equipos faltantes; mejora gradual conforme avanza el torneo |
| Bracket eliminatorio no muestra equipos | Dependen del resultado de fase de grupos; football-data.org los expone con `Winner-of-X` placeholders | Una vez terminen los grupos, los nombres aparecen automáticamente |
| Cron exacto puede desfasarse 5-15 min | Política de Actions bajo carga alta | Tolerable: el usuario consulta en tiempo de vida humana |

---

## 11. Roadmap (próximas mejoras)

| Prioridad | Feature |
|---|---|
| 🔴 Alta | Activar notificaciones por Telegram (configurar bot + chat_id) |
| 🟡 Media | Dashboard "complejo" estilo bayesiano: tabs por grupo/matchday/fecha con cards detallados (cuotas + forma + tarjetas + lesiones por partido) |
| 🟡 Media | Bracket eliminatorio dinámico que se rellena solo cuando terminen los grupos |
| 🟢 Baja | Cobertura completa de cuotas con tier de pago de The-Odds-API |
| 🟢 Baja | Notificaciones por email (SMTP) además de Telegram |

---

## 12. Cómo usar (para el usuario)

### Día a día
1. Doble clic en `viewer/index.html`.
2. El visor descarga los 4 JSON desde GitHub.
3. Filtros: equipo, fechas, **botón "Hoy"** para ver solo los partidos del día.
4. Las pestañas Cuotas / Stats / Lesiones se llenan automáticamente.

### Forzar actualización inmediata
- En GitHub: **Actions → Update World Cup 2026 results → Run workflow**.
- O por terminal: `gh workflow run update-results.yml --repo LEXsB/worldcup-2026`.

### Editar los canales de TV
- Edita [`data/broadcasts_co.json`](data/broadcasts_co.json) y haz commit + push. El visor se actualiza al recargar.

### Mantenimiento
- Si un equipo aparece sin stats: revisa que su nombre canónico esté en [`data/team_aliases.json`](data/team_aliases.json) y dispara un workflow run.
- Si una fuente cae: el validador abre un Issue automáticamente y el visor sigue mostrando el último JSON válido.

---

*Generado automáticamente como parte del proyecto. Mantenedor: @LEXsB.*
