# Handoff — WSC Simulation Challenge 2026, equipo UVG SIM

Documento de traspaso para una sesión nueva de Claude. Todo lo que está aquí fue
verificado midiendo o leyendo código en la sesión del 4–5 y 22–28 de septiembre
de 2026. **No re-derivar nada de esto.** Lo que es hipótesis está marcado como tal.

Incluye los aciertos y los errores. Los errores están porque repetirlos cuesta
corridas de cinco horas cada una.

---

## 1. Lo primero, porque invalida todo lo demás si se ignora

**La métrica que puntúa el reto es la pérdida de resiliencia acumulada (loss),
no el ATT promedio.**

```
loss = Σ sobre los 72 períodos de  (1 − ATT_baseline / ATT_corrida) × días_del_período
```

Está implementada en `dashboard/app.js` (`calculateResiliencePeriods`) y hay tests
de `resilience_kpi_calculator`. El dashboard la muestra arriba a la derecha como
*Cumulative Resilience Loss*. **Menor es mejor.**

Yo pasé una sesión entera optimizando el ATT promedio antes de darme cuenta. Las
dos métricas **ordenan las estrategias de forma distinta**: mi estrategia
adaptativa parecía empatar con el default por ATT (15.53 vs 15.60) y en loss
estaba claramente peor (40.3 vs 34.6). Si una sesión nueva empieza mirando el ATT,
va a perder el tiempo igual que yo.

### Tres propiedades de la métrica que cambian el diseño

**Un período más rápido que el baseline resta loss.** No es solo aguantar las
disrupciones: ir más rápido que el baseline en *cualquier* período genera crédito
negativo. El baseline es la estrategia por defecto sin disrupciones, así que
cualquier mejora de ruteo en operación normal paga.

**Es asimétrica y premia la velocidad.** Ser 20% más lento cuesta 0.167 por día;
ser 20% más rápido acredita 0.250 por día. Premia la agresividad, no la prudencia.

**Todos los períodos valen igual.** Los ~250 días sin disrupción pesan tanto como
los ~110 con ella.

### Cómo puntuar

`loss.py` (en el repo) lo calcula desde cualquier curva ATT por período:

```
python loss.py <baseline.csv> <corrida.csv> --detalle
```

`--detalle` separa penalización de crédito y lista los períodos que más pesan. Un
total dice *cuánto* se perdió; el desglose dice *dónde*, que es lo que permite
intervenir en vez de optimizar a ciegas.

**El baseline tiene que ser de la misma semilla que la corrida.** Puntuar una
corrida de la semilla 2027 contra el baseline commiteado (que es de la 2026) infla
el resultado: nos dio 11.08 en vez de 8.95. Para cada semilla nueva hay que generar
su baseline con `run_single.py --baseline --no-strategy`.

---

## 2. El reto y el equipo

Simulación de eventos discretos (librería local O2DESPy) de una red marítima de
contenedores. Objetivo: minimizar el loss bajo disrupciones.

**Solo se puede editar `response_strategies/user_strategy.py`** y archivos nuevos
que se agreguen dentro de `response_strategies/`. NO se tocan `main.py`,
`simulation_model/`, `scenario_builders/`, `config/` ni los CSV de `Input/`.
(Verificado: la entrega de Ronda 2 cumple — solo cambió `response_strategies/`
y el README.)

**Los cuatro puntos de decisión** que el simulador ofrece:

| Función | Se llama desde | Cuándo |
|---|---|---|
| `assign_associated_bookings` | `shipment_waiting_for_loading_at_origin_port.py` | al crear la cadena de reservas |
| `adjust_bookings_before_cargo_handling` | `vessel_queuing_for_berth.py` | **único** punto de replanificación en tránsito |
| `create_alternative_service_routes` | ambos | junto a las anteriores |
| `select_vessel_for_berth` | `berth_idle.py` | solo si `esperando >= muelles × 3` |

Devolver `None` en cualquiera cede la decisión a `DefaultStrategy`.
`strategy_validation.py` prohíbe crear o eliminar buques y legs en rutas
alternativas.

**Equipo:** usuario `Tanktorm` (Andrés Ortiz). Compañeros: `alemanuel18`
(Alemanuel), Omar Pérez, Dulce. Idioma de trabajo: español.

**Repositorios:**
- `Tanktorm/Winter2Sim` — el del usuario. **Todo lo que produzca una sesión va aquí, en ramas nuevas.**
- `alemanuel18/SimulationChallenge2026_Py_Round2` — donde trabaja el equipo. El usuario tiene permiso de escritura.

**Regla explícita del usuario:** *"tienes estrictamente prohibido cambiar algo
sobre las otras ramas, solo puedes analizarlas y tomarlas en cuenta; si quieres
experimentar crea una nueva rama, preferiblemente en Tanktorm."* Respetar esto.

---

## 3. La red de la Ronda 2

20 puertos, 9 rutas, 41 buques, todos a 20 nudos.

| Ruta | Nombre | Buques | Ciclo | Headway | Espera media |
|---|---|---|---|---|---|
| S1 | Asia-Europe-NorthEurope | 8 | 46.3 d | 5.79 d | 2.90 d |
| S2 | Intra-EastAsia | 2 | 7.1 d | 3.57 d | 1.78 d |
| S3 | Mekong-Regional-Feeder | 2 | 3.9 d | 1.95 d | 0.98 d |
| S4 | Transpacific-South | 4 | 26.1 d | 6.52 d | 3.26 d |
| S5 | Asia-US-East | 9 | 53.2 d | 5.91 d | 2.95 d |
| S6 | Transatlantic-Caribbean | 5 | 27.1 d | 5.42 d | 2.71 d |
| S7 | IndianOcean-Gulf-EastMed | 5 | 28.9 d | 5.79 d | 2.89 d |
| S8 | Indonesia-Regional-Feeder | 1 | 2.2 d | 2.25 d | 1.12 d |
| S9 | Transpacific-North | 5 | 25.3 d | 5.06 d | 2.53 d |

Muelles: Shanghai 3, Singapore 3, Rotterdam 3; Shenzhen/Qingdao/Busan/Kaohsiung/
Colombo/Jebel Ali/Hamburg/Piraeus/Los Angeles/New Jersey/Tanger Med 2; Tianjin,
Xiamen, Laem Chabang, Ho Chi Minh, Jakarta, **Cartagena** 1.

**Cartagena es nodo hoja**: solo la sirve S6, y solo desde New Jersey.

### Las disrupciones (días relativos al inicio de la medición)

| Tipo | Dónde | Días | Efecto |
|---|---|---|---|
| Tramo | Colombo → New Jersey | 40–100 | ×5 |
| Tramo | Shanghai → Kaohsiung | 140–200 | ×5 |
| Tramo | Qingdao → Busan | 215–240 | ×5 |
| Cierre | **Piraeus** | 260–274 | cerrado |
| Cierre | Tianjin | 320–327 | cerrado |

Están en `scenario_builders/disruption_scenario.py` sumadas al warm-up (140 días),
así que en tiempo absoluto son +140.

**La dominante es Colombo→New Jersey:** 9,000 nm que pasan de 18.75 a 93.75 días de
navegación, durante 60 días, sobre el único brazo Asia–US-East. El desvío existe:
Asia →S1→ Tanger Med →S6→ New Jersey.

**Piraeus es el punto débil persistente.** Es hub de transbordo de S1 y S7. Su
cierre genera una estela de penalización en los días ~286–320 que aparece **en las
dos semillas medidas**, o sea que es estructural, no ruido.

---

## 4. Referencias numéricas (semilla 2026 salvo que se diga)

| Corrida | loss | ATT |
|---|---|---|
| **Cota física de la red** | — | **11.71 d** |
| Baseline sin disrupciones | 0 (por definición) | 13.854 |
| Disrupción + `DefaultStrategy` | **34.574** | 15.532 |
| Estrategia adaptativa mía (descartada) | 40.298 | 16.256 |
| Alemanuel, versión del 4-sep | 3.296 | 14.095 |
| Alemanuel + parche de estela (mío) | **3.103** | 14.086 |
| **Alemanuel, entrega final (21-sep)** | **0.614** | 13.950 |

La **cota física** son 11.71 días de navegación pura a 20 nudos, ponderada por
demanda, sin esperas ni atraques ni transbordos. Ninguna estrategia puede bajar de
ahí. Sirve para detectar resultados imposibles: alguien reportó "3.30 de ATT" y era
un loss, no un ATT.

Semilla 2027: baseline 13.961, Alemanuel+parche 8.954 (ATT 14.401).

### El dato más importante sobre variabilidad

**El loss varía ~6 puntos entre semillas.** La misma estrategia dio 3.103 en la
2026 y 8.954 en la 2027. Las mejoras que estábamos midiendo eran de 0.2.

Consecuencia: **ningún número de una sola semilla describe una estrategia.** Y la
entrega final (0.614) está calibrada por Optuna usando **solo la semilla 2026**
(`optimize_round2.py` líneas 182 y 368). Es casi seguro que está sobreajustada a
esa realización.

---

## 5. Qué funciona y qué no (todo medido)

### Funciona

**Rutear por tiempo esperado en vez de distancia.** El default hace Dijkstra sobre
`edge.total_distance` (`default_strategy.py:852`), así que no ve el multiplicador
de congestión y reserva carga en tramos de 93 días.

**Evaluar el multiplicador en el instante futuro de paso**, no al reservar. El
desvío se activa y se apaga solo, sin fechas codificadas.

**Respetar el plan de reservas en tránsito.** Si `adjust_bookings_before_cargo_handling`
devuelve `None`, el default rehace la cadena con distancia pura y deshace el
desvío. Impedírselo valió **0.57 días de ATT** en mi estrategia.

**No retener carga por destino cerrado sin comparar.** Los cierres duran 7–14 días
y los tránsitos 14–40. Rechazar toda reserva durante el cierre acumula la carga en
origen y la libera de golpe al reabrir. Corregirlo valió **3.296 → 3.103**.

### No funciona

**Ponderar la cola** por (ruta, punto de embarque): 15.856 con peso 0, 16.786 con
0.2, 16.256 con 1.0. No es cuestión de elegir un peso menor — el mecanismo se
realimenta: la cola encarece el buen camino, la carga se va a uno peor, ese se
satura. **Descartado en todos los pesos probados.**

**Quitar la espera de embarque y el transbordo** del costo: ATT 27.13. Esos
términos son los que más aportan.

**Excluir las rutas alternativas** del default del grafo de reserva: resultado
idéntico, 15.8564. Sin efecto.

**Replanificar con costo propio en cada llegada** (`WSC_REPLAN_MODE=own`): correcto
en principio, inviable en cómputo — más de 2 horas sin terminar el warm-up. Si se
retoma, hay que filtrar: replanificar solo los envíos cuyo plan aún cruza una
disrupción.

**Priorizar la asignación de muelles**: la cola promedio es de 0.18–0.20 buques y
`select_vessel_for_berth` casi nunca se invoca (umbral = muelles × 3). Cero retorno.

---

## 6. Mis errores, para que no se repitan

**Medí la métrica equivocada durante toda una sesión.** Optimicé ATT promedio sin
verificar cuál era el criterio del reto. Estaba en `dashboard/app.js` desde el
principio. *Lección: antes de optimizar, leer cómo se puntúa.*

**Declaré una mejora con dos semillas.** Con 2026 y 2027 mi estrategia parecía
mejorar 0.20 días de forma consistente; la tercera (2028) invirtió el signo y el
promedio quedó en −0.066 ± 0.237, indistinguible de cero. Estuve a punto de que el
usuario lo presentara. *Lección: tres semillas mínimo, y comparar siempre contra el
default en la misma semilla.*

**Puntué una semilla contra el baseline de otra.** Dio 11.08 en vez de 8.95. *Lección:
cada semilla necesita su propio baseline.*

**Puse los pesos a ojo y nunca los calibré.** `run_batch.py` existe para eso y no
llegó a usarse en serio. Las corridas se fueron en diagnosticar.

**Tres de mis cuatro hipótesis sobre el déficit resultaron falsas** (cola,
retención, rutas alternativas). La cuarta —la replanificación en tránsito— fue la
correcta, y la encontré **leyendo el código y comparándolo con el del default**, no
midiendo. *Lección: en un sistema con esta realimentación, la intuición no predice;
pero leer el código adversarialmente sí encuentra defectos.*

**Di un comando de verificación que fallaba** (`import response_strategies.adaptive_strategy`
dispara una importación circular que ya existe en el proyecto). El usuario creyó
que su instalación estaba rota. La verificación buena es
`import simulation_model, response_strategies.X` o directamente la prueba corta.

**Propuse un plan de 6 configuraciones sin saber la velocidad de su máquina.**
Habría necesitado 24 horas. *Lección: medir el tiempo por corrida antes de planear.*

---

## 7. Herramientas construidas (todas en el repo)

| Archivo | Qué hace |
|---|---|
| `loss.py` | Calcula el loss oficial desde curvas ATT. `--detalle` desglosa penalización, crédito y períodos dominantes. |
| `run_single.py` | Una corrida, guarda la curva ATT período a período en `Output/Analysis/att_<label>.csv`. Flags: `--seed`, `--days`, `--warmup`, `--no-strategy`, `--baseline`, `--dashboard`. |
| `compare_runs.py` | Compara varias curvas: media, desviación, mejor y peor período. |
| `run_batch.py` | Calibración desatendida por horas, reanudable, resultados incrementales en `Output/Batch/results.csv`. **Nunca se usó en serio.** |

`run_single.py --dashboard` escribe además los CSV de `Output/` que lee el
dashboard, para no pagar una segunda corrida solo por verlo.

**Trampas de formato de los CSV de salida**, ya resueltas en estas herramientas:
miles entre comillas (`"1,190"`), celdas `-` en matrices OD, filas `TOTAL`,
filas de resumen con columnas vacías, porcentajes como texto.

---

## 8. Mapa de ramas

**`Tanktorm/Winter2Sim`:**

| Rama | Contenido |
|---|---|
| `claude/winte2sim-strategy-tytkek` | Mi estrategia adaptativa (**descartada**, loss 40.3), análisis, `loss.py`, herramientas. PR #2. |
| `propuesta/replan-transito` | Estrategia de Alemanuel + parche de estela + control de la replanificación en tránsito (`WSC_REPLAN_MODE`: keep/adaptive/default). **Sin medir.** |
| `propuesta/calendario-real` | **`chrono_strategy.py`** — estrategia propia, ruteo por calendario real de salidas. **Sin medir.** |

**`alemanuel18/SimulationChallenge2026_Py_Round2`:**

| Rama | Contenido |
|---|---|
| `propuesta/alemanuel1` | La estrategia del equipo. Es la entrega. loss 0.614. |
| `mejora/estela-3.10` | El parche de estela del usuario (loss 3.103). |

---

## 9. Las dos propuestas sin medir

### A. Replanificación en tránsito (`propuesta/replan-transito`)

`user_strategy.adjust_bookings_before_cargo_handling` devuelve `None` en la
estrategia del equipo, así que **el default rehace sus cadenas con distancia pura
en cada llegada a puerto**, deshaciendo los desvíos. En mi estrategia ese mismo
defecto valía 0.57 días de ATT.

Modo `keep` (defecto): un `return True`, cero cómputo extra. Modo `adaptive`:
rehace el tramo pendiente con el costo propio, solo para los envíos cuyo plan aún
cruza una disrupción.

**Riesgo:** en `keep`, un plan que se vuelva inviable ya no se corrige.

### B. Calendario real de salidas (`propuesta/calendario-real`)

Estrategia original, desde `main`. La red tiene 2.15 días de sobrecosto sobre la
cota física y **no son navegación, son espera**: los headways troncales son de 5 a
6.5 días, y una cadena con dos transbordos paga 6–9 días esperando barcos.

Todas las estrategias anteriores modelan esa espera como medio headway promedio.
Pero cada servicio tiene rotación fija, `StartDayOfWeek` publicado y buques
repartidos por el ciclo: **las salidas son derivables**. `chrono_strategy.py` las
calcula y busca el camino que llega antes en ese calendario, con un Dijkstra
dependiente del tiempo (la propiedad FIFO se cumple, así que Dijkstra es válido).

Verificado sobre el grafo sin simular: en el día 200 desvía Shanghai→New Jersey por
Tanger Med y S6; en el día 300 abandona S4 y va a Los Angeles por Busan y S9; el
mismo par OD da tiempos distintos según el día, que es el calendario funcionando.

**Por qué debería bajar loss y ATT a la vez:** cada punto porcentual de mejora en
operación normal vale ~−3.6 de crédito repartido en los 72 períodos. El margen
hasta la cota física es del 15.5%. Alemanuel saca −6.75 de crédito (≈2%); el
default −0.42.

**Riesgo principal:** el calendario es una *derivación* que supone reparto uniforme
de buques por el ciclo. Si el simulador los coloca de otra forma, las fases quedan
desfasadas y elegirá conexiones que no existen — y entonces rendiría peor que el
promedio de medio headway. Diagnóstico: si el ATT sube mucho, las fases están mal.

---

## 10. La Ronda 3 es a ciegas

Según la reunión con el equipo anterior: la solución se entrega **antes** de
conocer el escenario, no se sabrá qué rutas o nodos se bloquean ni en qué días, y
se evalúa en un escenario desconocido igual para todos.

**Esto es deuda grave en la entrega actual.** La estrategia del equipo tiene:

```python
_ROUND2_LEG_EVENTS = {("colombo","new jersey"), ("shanghai","kaohsiung"), ("qingdao","busan")}
_ROUND2_CLOSED_PORTS = {"piraeus", "tianjin"}

def is_round_two_context(context) -> bool:
    ...
    return leg_events == _ROUND2_LEG_EVENTS and closed_ports == _ROUND2_CLOSED_PORTS
```

Si el escenario cambia, **las 1079 líneas se vuelven un `return None`** y compiten
con el default. Además los parámetros son **por ruta con nombre**
(`lead_margin_s5`, `port_lead_margin_s7`, `enable_s4_detour`…) con valores como
`"s5": 9.994766349122587`, ajustados a la geografía de esta ronda.

**La corrección general:** parámetros en función de *propiedades* de la ruta —su
headway, su número de buques, la severidad del tramo afectado, cuántas rutas
sirven ese puerto— en vez de su identidad. Mismo poder expresivo, y se transfiere.
Y sustituir el guardia por una condición estructural (`hay disruption_plans`),
validando que no empeore el baseline.

`chrono_strategy.py` ya está escrita así: cero nombres, cero fechas, todo derivado
de `Input/` y de `context.disruption_plans`. **El calendario existe en cualquier
escenario**, así que su ventaja no depende de qué se rompa. Es la apuesta que mejor
transfiere.

Otros puntos de la reunión que no se trabajaron: **vigilar que la flota no quede
atrapada** cuando un cierre parte la red en dos (no aparece en el ATT promedio
hasta que es tarde), y **degradación segura** — ante cualquier situación no
prevista, devolver `None` y dejar al default, porque una estrategia que falla en un
escenario desconocido puntúa peor que no tener estrategia.

---

## 11. Restricciones operativas

**La máquina del usuario:** 2 núcleos, 8 GB RAM, Python 3.13 de 64 bits en
`C:\Users\Andres Ortiz\AppData\Local\Programs\Python\Python313\`. Proyecto en
`C:\Users\Andres Ortiz\Documents\winter\SimulationChallenge2026_Py_Round2`, cuyo
`origin` apunta al repo de alemanuel18.

**Una corrida completa (140 + 360 días) tarda 4–6 horas.** Una sola a la vez. Eso
significa ~1–2 experimentos por día. **Cualquier plan que requiera decenas de
corridas es inviable ahí** — la calibración por búsqueda aleatoria necesita 40+.

Fórmula para estimar el total desde las dos primeras líneas de progreso:
`total ≈ (t_día120 − t_día60) × 6 + t_día60`. Falla por menos del 2%.

**Usar siempre la ruta directa del intérprete**, `.\.venv\Scripts\python.exe`, en
vez de activar el entorno: la política de ejecución de PowerShell bloquea
`Activate.ps1` y ya costó varios intentos.

**Las variables de entorno no sobreviven a una ventana nueva.** Si una prueba
necesita `$env:WSC_...`, hay que ponerlo en la misma ventana, antes de correr.

**No pegar varios comandos juntos en PowerShell**: una corrida de 3 horas se perdió
porque el bloque pegado interfirió y la cortó con `KeyboardInterrupt`.
`run_single.py` escribe el CSV **solo al terminar**, así que una corrida
interrumpida se pierde entera.

**El contenedor remoto de Claude se recicla cada ~45 minutos.** Dos intentos de
dejar `run_batch.py` corriendo aquí murieron sin escribir una sola fila. **No
intentar corridas largas en este entorno** — la máquina del usuario es la única
fiable. Los archivos del working dir sobreviven, pero los procesos no.

---

## 12. El incidente de integridad (28 de septiembre)

Contexto necesario para entender el estado del equipo.

Los organizadores escribieron pidiendo aclaración: la implementación de la entrega
de Ronda 2 es "sustancialmente idéntica" a la de otro equipo, y **ambas produjeron
los mismos resultados** en su evaluación. Preguntaron si hubo colaboración,
compartición de código u otra conexión.

Lo que se estableció: miembros del equipo conversaron con participantes del otro
equipo describiendo problemas encontrados; y **ambos equipos usaron el asistente de
IA de la universidad**, lo que explicaría implementaciones convergentes. Alemanuel
y Dulce confirmaron el uso de esa herramienta.

Redacté una respuesta honesta que declaraba que no se intercambió código y
divulgaba el uso del asistente institucional. **Dulce pidió quitar ese párrafo y
"fingir que no saben del otro equipo ni del ChatGPT de la U". Me negué** a redactar
esa versión y ofrecí una más corta que no especula sobre el otro equipo pero
mantiene el hecho declarado.

**Para una sesión futura:** no redactar declaraciones que finjan desconocimiento de
hechos conocidos ante un comité de integridad. Si el tema vuelve, la posición es la
misma. El argumento práctico que le di al usuario: al otro equipo le están haciendo
la misma pregunta, y una contradicción convierte una coincidencia explicable en una
ocultación deliberada.

---

## 13. Pendientes, por valor esperado

1. **Medir `propuesta/calendario-real`** (semilla 2026, contra loss 0.614). Es la
   apuesta con más techo y la única que transfiere garantizado a la Ronda 3.
2. **Medir `propuesta/replan-transito` en modo `keep`.** Cero costo de cómputo,
   corrige un defecto identificado en el código.
3. **Probar `WSC_ENABLE_S1_BYPASS=1`** sobre la entrega actual. Parámetro que ya
   existe, ataca la ventana de Piraeus (días 286–320) que domina en ambas semillas.
   Una corrida parcial mostró ATT 13.07/13.12/13.35/13.40 en los días 60/120/180/240
   —muy por debajo del baseline— pero se interrumpió antes de la parte que decide.
4. **Generalizar para la Ronda 3**: quitar el guardia, parametrizar por propiedades
   de ruta. Más importante que exprimir décimas en la Ronda 2.
5. **Confirmar con 3 semillas** cualquier cosa que parezca funcionar, con el
   baseline propio de cada una.

### Protocolo de medición, no negociable

1. Puntuar con el **loss**, nunca con el ATT promedio.
2. Reportar la **media de los 72 períodos**, nunca un período suelto. (Dos números
   falsos circularon en este proyecto por este error: "12.40" y "3.30".)
3. **Tres semillas mínimo**, cada una con su propio baseline.
4. Comparar contra el default **en la misma semilla**.
5. **Verificar los TEU completados.** Un loss que baja mientras la carga completada
   cae no es una mejora: es carga que dejó de moverse. (La entrega actual pasa esta
   prueba: 490,095 TEU contra 487,606 del default.)
6. Comprobar que el ATT esté por encima de **11.71**. Si no, es error de medición.
