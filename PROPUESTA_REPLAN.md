# Propuesta: tomar el control de la replanificación en tránsito

## El hallazgo

`user_strategy.adjust_bookings_before_cargo_handling` devuelve `None` en la estrategia actual. Ese es **el único punto de replanificación en tránsito** del simulador, y se llama cada vez que un buque llega a un puerto, antes de manipular la carga.

Al devolver `None`, la decisión cae en `DefaultStrategy`, que **rehace la cadena de reservas pendiente con su métrica de distancia pura** — la misma que la estrategia está reemplazando en `assign_bookings`. Es decir: se calcula un desvío cuidadoso al reservar, y el default lo deshace en la primera escala.

El mismo defecto, en la estrategia adaptativa de esta rama, valía **0.57 días de ATT**: 15.856 con la replanificación del default contra 15.285 impidiéndosela, con la semilla 2026. Sobre una estrategia mejor calibrada debería valer más, porque hay más que deshacer.

Encaja además con la forma del loss restante: la estela concentra 5.78 de los 11.45 puntos de penalización, y la estela es precisamente carga que fue reservada durante una disrupción y **replanificada en tránsito** mientras la disrupción seguía activa.

## Los tres modos

Bajo `WSC_REPLAN_MODE`:

| Modo | Qué hace | Costo de cómputo |
|---|---|---|
| **`keep`** (nuevo defecto) | Respeta el plan asignado. Nadie lo toca en tránsito. | **Cero.** Un `return True`. |
| `adaptive` | Rehace el tramo pendiente con el costo propio, **solo** para los envíos cuyo plan todavía cruza una disrupción. | Moderado, acotado por el filtro. |
| `default` | El comportamiento actual, para poder comparar. | El de hoy. |

El filtro de `adaptive` es lo que lo hace viable. Una versión anterior que replanificaba en cada llegada, sin filtro, no terminó el warm-up en más de dos horas: un Dijkstra por envío a bordo por escala es inviable. Aquí `_plan_crosses_disruption` descarta con una comprobación barata los envíos que no lo necesitan, que son la enorme mayoría.

## Orden de pruebas, pensado para una máquina de 2 núcleos

Cada corrida son ~5 horas y solo cabe una a la vez. Por eso el orden importa: primero lo que no cuesta nada y puede valer mucho.

**Corrida 1 — `keep`.** Es el defecto del archivo nuevo, no hay que configurar nada:

```
python run_single.py --label keep_s2026 --seed 2026
python loss.py Output/Baseline_ATT_By_Statistics_Interval.csv Output/Analysis/att_keep_s2026.csv
```

Objetivo: bajar de **3.103**. No añade ni un microsegundo de cómputo — al contrario, ahorra el trabajo que hacía el default al replanificar. Si funciona, es la mejora más barata disponible.

**Corrida 2 — `adaptive`**, solo si la 1 mejoró o quedó igual:

```
$env:WSC_REPLAN_MODE="adaptive"
python run_single.py --label adaptive_s2026 --seed 2026
```

Vigila el tiempo: si a los 90 minutos no ha salido del warm-up, córtala. Significa que el filtro no está descartando lo suficiente y hay que ajustarlo antes de insistir.

**Corrida 3 — confirmación** de la que haya ganado, en la semilla 2027, contra el baseline propio de esa semilla.

## Por qué esto y no calibrar

Calibrar los pesos existentes necesita decenas de corridas para explorar un espacio de cinco dimensiones. A cinco horas cada una, son semanas. Esta propuesta cambia **una decisión estructural** que hoy está cedida al default, y se evalúa con una sola corrida. Es la mejor relación valor/cómputo que queda sobre la mesa.

## Riesgo

En modo `keep`, un envío cuyo plan se vuelve inviable ya no se corrige: nadie lo replanifica. La estrategia sí evalúa las disrupciones futuras al reservar, así que el plan nace informado, pero puede haber casos límite. Si `keep` empeora el resultado, ese es el motivo, y entonces `adaptive` es la respuesta correcta: replanifica, pero con el costo bueno en vez del de distancia.
