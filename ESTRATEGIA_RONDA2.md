# Estrategia Ronda 2 (Winter2Sim) — propuesta

## 1. Números verificados de este repo (no re-derivar)

| Escenario | ATT | Fuente |
|---|---|---|
| Baseline sin disrupción | **13.85 d** | media de las 72 filas de `Baseline_ATT_By_Statistics_Interval.csv` |
| Disrupción + `DefaultStrategy` | **15.53 d** (+1.68, +12.1%) | `ATT_By_Statistics_Interval.csv` (σ entre períodos 1.99) |

`user_strategy.py` está vacío (todo `return None`) ⇒ la corrida subida es **100% default**. Ese 15.53 es el número a batir.

Otros KPIs de esa corrida: 9,894 TEU esperando; buques esperando muelle **0.20** (irrelevante); utilización global de rutas **3.5%** (sobra capacidad: el cuello es ruteo y frecuencia, no barcos). `S7-ALT-1` reservó 8,000 TEU de capacidad y transportó **0 TEU** — un buque secuestrado sin uso.

## 2. Red de Ronda 2 (cambió respecto a Ronda 1)

9 rutas / 41 buques. S8 = 1 buque (Singapore↔Jakarta, el menos frecuente). Cartagena sigue siendo hoja (solo S6). Nuevo: **S9 Transpacific-North** (Tianjin-Qingdao-Busan-LA) da alternativa real a S4 en el Transpacífico.

## 3. Jerarquía de las disrupciones (aquí está el 80% del ATT)

| Disrupción | Días | Impacto real | Palanca |
|---|---|---|---|
| **Colombo→New Jersey ×5** (S5) | 40–100 | 9,000 nm @20kt = 18.75 d → **93.75 d**. 60 días sobre el único brazo Asia→US-East. **Es la disrupción dominante.** | **Desvío estructural**: Asia →S1→ Tanger Med →S6→ New Jersey. Existe y no está tocado. |
| Shanghai→Kaohsiung ×5 (S4) | 140–200 | 531 nm: 1.1 d → 5.5 d (+4.4 d) | Kaohsiung por S2 (Shenzhen→Kaohsiung); Transpacífico por S9 vía Busan |
| Qingdao→Busan ×5 (S9) | 215–240 | 429 nm: 0.9 d → 4.5 d (+3.6 d) | S2 (Kaohsiung→Busan) |
| Cierre Piraeus | 260–274 | S1 y S7 (ambas lo tocan) | solo **timing**: no mandar carga que llegaría en la ventana |
| Cierre Tianjin | 320–327 | S2 y S9 | solo timing |

Regla que se deriva sola: **congestión de tramo = problema de ruteo; cierre de puerto = problema de timing.** Son dos mecanismos distintos y hay que atacarlos con código distinto.

## 4. Por qué falla el default (leído en `default_strategy.py`)

`_find_shortest_booking_path` hace Dijkstra sobre `edge.total_distance` (línea 852): **solo distancia**. Consecuencias:
- Ignora el multiplicador de congestión ⇒ mete carga en Colombo→NJ aunque cueste 93 días.
- Ignora espera de embarque, transbordo y cola ⇒ prefiere caminos "cortos" en un servicio infrecuente.
- Su reacción a la disrupción es crear rutas ALT (roba buques a rutas sanas: caso S7-ALT-1 con 0 TEU).

## 5. La estrategia propuesta

**Todo el costo en horas. Nunca sumar TEU ni % a horas.**

```
costo(arista) = sailing_h × mult_esperado(leg, t_llegada)
              + espera_embarque_h
              + transbordo_h        (solo si cambia de ruta)
              + cola_h
```

**(a) `assign_associated_bookings` — el 80% del valor.** Dijkstra sobre el estado **`(puerto, ruta)`** (seguir en la misma ruta no paga transbordo; cambiar sí) con ese costo. Reutilizar `_build_all_candidate_bookings` del default y solo cambiar la métrica.

**(b) Multiplicador prospectivo (la clave de Ronda 2).** No evaluar la disrupción "ahora", sino **en el instante estimado en que el contenedor recorrería ese tramo** (`t_actual + tiempo acumulado hasta ahí`). Con esto Colombo→NJ se descarta durante los días 40–100 y se vuelve a usar el día 101 sin ninguna regla codificada a mano. Lo mismo evita mandar carga a Piraeus/Tianjin dentro de su ventana de cierre.

**(c) Espera de embarque real.** `headway/2` castiga injustamente a servicios de 1 buque (S8, y en Ronda 1 mató a S7). Usar la **próxima salida real** del calendario de la ruta desde ese puerto (con fallback a `headway/2` si no se puede calcular). Dejarlo como bandera categórica para que el optimizador decida.

**(d) Cola en horas, por *Puerto+Ruta*, no por puerto.** `cola_h = (TEU_esperando_esa_ruta / capacidad_efectiva_por_salida) × headway_h`. Contar todos los TEU del puerto mezcla flujos que no compiten.

**(e) `adjust_bookings_before_cargo_handling` — con freno.** Replanificar solo si el ahorro supera `REROUTE_THRESHOLD_HOURS` **y** no se ha excedido una cuota de replanificaciones por (ruta, ventana de tiempo). Sin ese freno aparece migración en manada: todos se van al mismo desvío y lo saturan. Este paso va **después** de demostrar que (a)+(b) ya bajaron el ATT.

**(f) `create_alternative_service_routes` → devolver lista vacía / no crear.** Con 3.5% de utilización, crear rutas ALT no agrega capacidad útil y sí resta frecuencia (S7-ALT-1: 8,000 TEU ociosos). Dejarlo como **bandera booleana** del optimizador en vez de decidirlo por corazonada.

**(g) `select_vessel_for_berth` → no tocar.** 0.20 buques esperando; casi nunca se invoca (umbral = muelles × 3, y los puertos grandes tienen 2–3 muelles). Cero retorno.

## 6. Parámetros para calibrar (Optuna)

Numéricos: `TRANSFER_BUFFER_HOURS` 0–48 · `QUEUE_WEIGHT` 0–3 · `REROUTE_THRESHOLD_HOURS` 12–72 · `MAX_TRANSFERS` 1–3 · `ANTICIPATION_DAYS` 0–15 · `CONGESTION_RISK_WEIGHT` 0–2.
Categóricos (rompen el techo estructural, no solo afinan números): `usar_calendario_real` (bool) · `crear_rutas_ALT` (bool) · `permitir_transbordo_en_hub_saturado` (bool).

**Antes de optimizar nada: medir σ del ATT con 3 semillas y parámetros fijos.** Ese número define qué diferencias son reales. Objetivo robusto = `media_ATT + 0.5σ` sobre 3 semillas fijas; validar los 3 mejores con 5 semillas nuevas.

## 7. Orden de ejecución (no cambiar cinco cosas a la vez)

| Exp | Cambio | ATT esperado |
|---|---|---|
| E0 | default (referencia) | 15.53 |
| E1 | (a) costo en horas + estado (puerto, ruta) | < 15.53 |
| E2 | + (b) multiplicador prospectivo | el salto grande (Colombo→NJ) |
| E3 | + (d) cola dinámica | |
| E4 | + (e) rerouting con freno | |
| E5 | Optuna | acercarse a 13.85 |

Criterios: ATT < 15.53 → < 15 → acercarse a 13.85. Bajar los 9,894 TEU esperando. Sin hardcodear nombres de puertos, rutas ni fechas: todo se lee de `context.disruption_plans`.
