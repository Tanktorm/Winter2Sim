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
| `phase_guard_before_cycles` | ver `DEFAULTS` | cuánto esperar después de un cierre/congestión antes de volver a espaciar | 0–1 |
| `phase_guard_after_cycles` | 1.5 | cuánto antes de un cierre/congestión dejar de espaciar | 0.5–2 |
| `phase_min_delay_days` | 0.75 | retraso mínimo durante el calentamiento | 0.4–1 |
| `replan_threshold_days` | 1.0 | cuánto antes tiene que llegar un plan nuevo para replanificar | 0.5–2 |
| `detour_lead_days` | 3.0 | anticipación de los desvíos de flota | 1–6 |
| `detour_min_gain_days` | 1.0 | ganancia mínima para activar un desvío | 0.5–3 |

Ya probados y descartados: `connect_buffer_days=0.1` (peor en la Ronda 2:
−19.19 contra −21.35) y el espaciado sin guardas durante todo el año (pierde 12
puntos alrededor del cierre de Piraeus).

## 4. Cómo pasar un ganador a la entrega

Cuando una variante gane según la sección 2, se copian sus valores a `DEFAULTS` en
`response_strategies/r3_strategy.py`, se corre otra vez `tune` con `actual:` para
confirmar que da lo mismo, y se hace commit. Los jueces no usan variables de
entorno: lo que cuenta es lo que está en `DEFAULTS`.
