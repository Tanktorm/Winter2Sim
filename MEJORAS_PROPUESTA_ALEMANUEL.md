# Revisión de `propuesta/alemanuel1` — qué conservar y qué mejorar

## 0. Primero, el número correcto

| Corrida | ATT (media de los 72 períodos) |
|---|---|
| Baseline sin disrupción | 13.85 d |
| Disrupción + `DefaultStrategy` | 15.53 d |
| **Disrupción + `round2_strategy`** | **14.43 d** |

**Mejora real: −1.10 d (−7.1%) sobre el default.** Es un buen resultado.

⚠️ El commit se llama "resultados 12.40", pero 12.40 no es el ATT de la corrida: es el valor de **un período de 5 días**. La media es 14.43, el mínimo 12.79 y el **máximo 19.11**. Hay que reportar 14.43 — si se presenta 12.40 al jurado y lo verifican, se cae toda la credibilidad del trabajo. Además es **una sola semilla**: con σ entre períodos de ~2 días, una diferencia menor a ~0.5 d no es distinguible del ruido.

## 1. Lo que está bien y no hay que tocar

- **Grafo de tiempo esperado en lugar de distancia** (`_find_expected_time_path`). Es la corrección de fondo: el default rutea con Dijkstra sobre `edge.total_distance` y por eso mete carga en Colombo→New Jersey aunque ×5 la convierta en 93 días de navegación.
- **Multiplicador prospectivo** (`_leg_multiplier_at(context, leg, when)` evaluado en el instante estimado de paso, no "ahora"). Correcto y es la pieza que hace que la estrategia se apague sola cuando la ventana termina.
- **Detour de flota completa** en vez de la ruta ALT de un solo buque del default. Se ve en los resultados: `S5-R2-DETOUR` movió 625 TEU y `S4-R2-DETOUR` 335, mientras que la `S7-ALT-1` del default transportó **0 TEU** con 8,000 de capacidad secuestrada.
- Cachés de path y de aristas, invalidadas por estado de rutas activas. Sin eso el Dijkstra por shipment sería inviable.
- `WSC_ROUND2_MODE` / `WSC_WAIT_WEIGHT` por variable de entorno: permite comparar sin tocar el simulador. Buena decisión.

## 2. Mejoras, en orden de valor

### M1 — Retener carga con destino cerrado es contraproducente (ganancia inmediata)

`assign_bookings` (línea ~200):

```python
if _port_is_closed(context, destination, now):
    return False
```

Si el destino está cerrado **en el instante actual**, el envío se queda en origen. Pero Piraeus cierra 14 días y un tránsito Asia→Piraeus son ~25 días de navegación: para cuando el barco llegue, el puerto lleva 11 días abierto. Se está reteniendo carga por un cierre que ya no existirá al llegar.

Corrección: comparar el cierre restante contra el tiempo de tránsito, no contra cero.

```python
closed_days = _closed_port_wait_days(context, destination, now)
if closed_days > 0:
    path = _find_expected_time_path(context, now, origin, destination)
    transit_days = sum(_expected_edge_days(...) for edge in path) if path else 0.0
    if transit_days < closed_days - _CLOSURE_SLACK_DAYS:
        return False   # llegaría en plena ventana: sí conviene esperar
    # si no, despachar: el puerto ya estará abierto
```

Sale gratis (el path ya se calcula igual dos líneas más abajo) y libera carga retenida durante las dos ventanas de cierre.

### M2 — `is_round_two_context` apaga la estrategia en cualquier otro escenario

```python
_ROUND2_LEG_EVENTS = {("colombo","new jersey"), ("shanghai","kaohsiung"), ("qingdao","busan")}
_ROUND2_CLOSED_PORTS = {"piraeus", "tianjin"}
```

La estrategia solo se activa si el escenario tiene **exactamente** las rutas S1..S9 y **exactamente** esas cinco disrupciones. En la siguiente ronda cambia un puerto y todo el archivo de 990 líneas se vuelve un `return None`: se compite con el default.

Es hardcoding del enunciado, y además el enunciado prohíbe depender de nombres concretos. La lógica de fondo (grafo de tiempo esperado + detours) **es general**: lee todo de `context.disruption_plans`. No necesita el guardia.

Sustituir por una condición estructural:

```python
def _strategy_applies(context) -> bool:
    return bool(context.disruption_plans) and len(context.initial_service_routes) >= 2
```

Y validar que con el escenario baseline (sin disrupciones) el ATT no empeore respecto al default. Ese es el único riesgo que el guardia estaba cubriendo, y se cubre mejor midiéndolo.

### M3 — El costo no tiene cola ni transbordo → migración en manada

`_expected_edge_days` = `0.5·headway + cierre + Σ(navegación×mult + atraque)`. Le faltan dos términos y ambos se notan en los resultados:

- **Transbordo**: cambiar de ruta no cuesta nada extra en el modelo, así que el planificador encadena transbordos libremente. Se ve en Colombo (213 TEU en transbordo, subió respecto al default) y Piraeus (253).
- **Cola**: `0.5·headway` asume que siempre cabes en el próximo barco. Todos los envíos del mismo par OD ven el grafo **idéntico** (además cacheado por día) y eligen el **mismo** camino, hasta saturarlo. El pico de 19.11 d es exactamente eso.

```python
if previous_route is not edge.route:
    elapsed += TRANSFER_BUFFER_HOURS / 24.0

waiting = teu_waiting_for(edge.departure_port, edge.route)   # por Puerto+Ruta, no por puerto
queue_cycles = waiting / max(1.0, capacity_per_departure(edge.route))
elapsed += QUEUE_WEIGHT * queue_cycles * headway
```

La cola por **Puerto+Ruta** (no todos los TEU del puerto) es lo que rompe el empate y reparte la carga entre caminos: en cuanto un camino se llena, sube su costo y el siguiente envío elige otro. Es el término que convierte un planificador determinista en uno que balancea.

*Nota sobre la caché:* al meter la cola, el estado deja de depender solo del día, así que la clave de `_round2_path_cache` debe incluir un cubo de ocupación (p. ej. TEU esperando redondeado a centenas) o expirar más seguido. Si no, se cachea la decisión saturada.

### M4 — `adjust_bookings_before_cargo_handling` sigue vacío

Es el único punto de replanificación en tránsito y hoy devuelve `None`. La ruta se fija al generar el envío y no se corrige nunca. Con M3 ya funcionando, replanificar si el ahorro supera un umbral (`REROUTE_THRESHOLD_HOURS`, arrancar en 24 h) recupera la carga que quedó atrapada detrás de una cola imprevista.

**Con freno obligatorio**: cuota máxima de replanificaciones por (ruta, ventana de tiempo). Sin ella el rerouting reproduce el mismo efecto manada que M3 intenta eliminar. Y va **después** de M1–M3, nunca antes.

### M5 — `0.5·headway` es un promedio, no una salida

`_expected_edge_days` usa media espera del headway. El simulador tiene el calendario real (`StartDayOfWeek` en `service_routes.csv` + posición de los buques). Usar la **próxima salida real** desde ese puerto elimina el sesgo contra las rutas de pocos buques (S8 tiene 1: paga ~medio ciclo de castigo permanente en cada evaluación).

Dejarlo como bandera `WSC_USE_REAL_SCHEDULE` para poder medir si realmente aporta.

### M6 — Calibrar, con protocolo

`WSC_WAIT_WEIGHT` ya existe. Al agregar M3–M5 hay 5–6 números libres. Optuna sobre:

`TRANSFER_BUFFER_HOURS` 0–48 · `QUEUE_WEIGHT` 0–3 · `WAIT_WEIGHT` 0.3–1.5 · `REROUTE_THRESHOLD_HOURS` 12–72 · `CLOSURE_SLACK_DAYS` 0–5, más las banderas categóricas (`USE_REAL_SCHEDULE`, `ENABLE_REROUTE`).

Protocolo: **antes de optimizar, medir σ del ATT con 3 semillas y parámetros fijos.** Objetivo `media + 0.5σ` sobre 3 semillas fijas; validar los 3 mejores con 5 semillas nuevas. Congelar los ganadores como constantes para la entrega.

## 3. Orden de trabajo

| Paso | Cambio | Costo | Riesgo |
|---|---|---|---|
| 1 | M1 (cierre vs tránsito) | 10 líneas | nulo |
| 2 | M2 (quitar el guardia + validar en baseline) | 5 líneas | bajo, hay que medirlo |
| 3 | M3 (transbordo + cola) | ~40 líneas | medio: revisar la caché |
| 4 | M5 (calendario real) | ~30 líneas | bajo, va con bandera |
| 5 | M4 (rerouting con cuota) | ~60 líneas | alto: solo si 1–4 mejoraron |
| 6 | M6 (Optuna) | script aparte | — |

Una corrida por paso, comparando siempre contra **14.43** (no contra 12.40). Meta: bajar de 14.43 → acercarse a 13.85 → superarlo. Y bajar el pico de 19.11, que es donde está el margen grande.
