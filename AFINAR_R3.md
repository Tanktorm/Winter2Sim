# Cómo seguir bajando el loss de r3

Guía para seguir mejorando `response_strategies/r3_strategy.py` con evidencia, sin
tocar el código: cada parámetro de `DEFAULTS` se puede cambiar por corrida, y el
banco ordena las configuraciones por el mismo score robusto que usa Alemanuel.

## 1. El comando

```powershell
.\.venv\Scripts\python.exe r3_bench.py tune --workers 7 --variant "actual:" --variant "prueba:phase_measure_min_delay_days=0.75"
```

- `--variant nombre:clave=valor,clave=valor` define una configuración. Se puede
  repetir. `actual:` (sin claves) es la configuración por defecto, la que se
  entregaría.
- `--scenarios` por defecto corre los 9 escenarios del banco (`r2,a1,a2,a3,a5,a6,a7,a8,h3`).
  Para una prueba rápida: `--scenarios r2,a2,h3`.
- `--seeds 2026,2027` repite todo con otra semilla.
- `--workers` es cuántas corridas a la vez: 7 en la máquina de Alemanuel, 2 en la de
  Andrés.

Al terminar imprime cada variante con su promedio, su peor caso y su score robusto,
ordenadas de mejor a peor. Las corridas ya hechas no se repiten, así que se puede
cortar con Ctrl+C y relanzar. Los baselines se calculan una sola vez por escenario y
semilla.

## 2. Regla para aceptar un cambio

1. Que baje el **score robusto** en los 9 escenarios, no solo el promedio.
2. Que no empeore el peor caso.
3. Que se mantenga con una segunda semilla (`--seeds 2026,2027`).
4. Que la carga completada (`TEU compl.` en `report`) no baje más de 1%.

Un cambio que gana en un escenario y pierde en otro casi siempre es ruido: el loss
varía varios puntos entre semillas.

## 3. Parámetros que vale la pena explorar

| Parámetro | Valor actual | Qué controla | Rango razonable |
|---|---|---|---|
| `phase_measure_min_delay_days` | ver `DEFAULTS` | retraso mínimo que justifica ajustar un buque durante la medición | 0.5–1.5 |
| `phase_measure_cooldown_cycles` | ver `DEFAULTS` | cada cuánto puede ajustarse el mismo buque (fracción del ciclo) | 0.15–0.5 |
| `phase_measure_max_extra_days` | 2.5 | máximo de días extra por ajuste | 1.5–4 |
| `phase_guard_before_cycles` | 0.25 | cuánto esperar después de un cierre/congestión antes de volver a espaciar | 0–1 |
| `phase_guard_after_cycles` | 1.5 | cuánto antes de un cierre/congestión dejar de espaciar | 1–2 |
| `phase_light_ratio` | 0.5 | un buque que sale con menos de esta fracción de su carga habitual en esa escala se espacia sin los límites del período medido | 0.3–0.7 |
| `phase_min_delay_days` | 0.75 | retraso mínimo durante el calentamiento | 0.4–1 |
| `replan_threshold_days` | 1.0 | cuánto antes tiene que llegar un plan nuevo para replanificar | 0.5–2 |
| `detour_lead_days` | 3.0 | anticipación de los desvíos de flota | 1–6 |
| `detour_min_gain_days` | 1.0 | ganancia mínima para activar un desvío | 0.5–3 |

Ya probados y descartados (loss, semilla 2026):

| Cambio sobre v2 | Resultado | Por qué se descartó |
|---|---|---|
| `connect_buffer_days=0.1` | r2 −19.19 (v2 −21.35) | peor |
| espaciado sin guardas todo el año | r2 −10.90 | pierde 12 puntos alrededor del cierre de Piraeus |
| `phase_guard_after_cycles=0.75` | r2 −20.53 (solo guarda 0.25: −25.77) | espaciar justo antes de un cierre se pierde |
| `phase_measure_cooldown_cycles=0.25` | a2 +0.15 (v2 −12.24) | ajustar seguido buques cargados retrasa su carga |
| `phase_measure_min_delay_days=1.0` | r2 −25.96, a2 −11.60 | gana en r2 y pierde en a2 |
| las dos anteriores juntas con guarda 0.25 | a2 +0.48 | mismo motivo |

Lo que sí se aceptó (v3) fue espaciar sin límites solo a los buques casi vacíos.
Ganó en todos los casos medidos; la tabla completa está en la sección 0 de
`RONDA3.md`.

## 4. Cómo pasar un ganador a la entrega

Cuando una variante gane según la sección 2, se copian sus valores a `DEFAULTS` en
`response_strategies/r3_strategy.py`, se corre otra vez `tune` con `actual:` para
confirmar que da lo mismo, y se hace commit. Los jueces no usan variables de
entorno: lo que cuenta es lo que está en `DEFAULTS`.

## 5. Próximas pruebas sugeridas

Sobre v3 quedan dos ideas con sentido, las dos sobre la recuperación después de un
cierre largo (en h3 todavía se pierden unos 17 puntos entre los días 120 y 210):

```powershell
.\.venv\Scripts\python.exe r3_bench.py tune --workers 2 --scenarios h3,a2,r2 --variant "actual:" --variant "liviano07:phase_light_ratio=0.7" --variant "guarda0:phase_guard_before_cycles=0"
```

- `liviano07`: más buques cuentan como casi vacíos, así que el grupo se deshace más
  rápido. Riesgo: empieza a retrasar buques con carga.
- `guarda0`: vuelve a espaciar apenas termina la disrupción.

Si una gana en los tres, se valida en los 9 escenarios y con `--seeds 2026,2027`
antes de cambiar `DEFAULTS` (sección 2).
