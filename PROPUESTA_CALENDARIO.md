# Propuesta: ruteo por calendario real de salidas

Estrategia propia, construida desde `main`. No deriva del trabajo de Alemanuel: comparte solo los hechos medidos sobre la red.

## 1. El diagnóstico que la motiva

| Referencia | ATT |
|---|---|
| Cota física de la red (navegación pura, ponderada por demanda) | **11.71 d** |
| Baseline sin disrupciones | **13.86 d** |

Hay **2.15 días de sobrecosto** que no son navegación. ¿De dónde salen? De esperar barcos:

| Ruta | Buques | Ciclo | Headway | Espera media |
|---|---|---|---|---|
| S1 | 8 | 46.3 d | 5.79 d | **2.90 d** |
| S4 | 4 | 26.1 d | 6.52 d | **3.26 d** |
| S5 | 9 | 53.2 d | 5.91 d | **2.95 d** |
| S9 | 5 | 25.3 d | 5.06 d | **2.53 d** |

Un contenedor con dos transbordos paga entre seis y nueve días esperando, contra unos doce navegando. **La espera es la mitad del problema y nadie la está atacando.**

## 2. La idea

Todas las estrategias hasta ahora —la mía anterior y la de Alemanuel— modelan la espera como **medio headway promedio**. Pero la espera no es un promedio: cada servicio tiene rotación fija, un `StartDayOfWeek` publicado y sus buques repartidos uniformemente por el ciclo. **Las salidas desde cada puerto son conocidas.**

Esta estrategia las calcula y busca el camino que **llega antes en ese calendario**, no el que es más corto en promedio.

La diferencia es concreta: elegir una conexión que sale en cuatro horas en vez de una que sale en cuatro días es un ahorro real, y es **invisible** para cualquier modelo que solo conozca la media. Con headways de 5–6 días, acertar o fallar una conexión cuesta casi una semana.

## 3. Cómo funciona

**El calendario.** Para cada ruta se deriva la fase de salida de cada segmento (offset publicado + tiempo de rotación hasta ahí) y el headway (ciclo ÷ buques). La próxima salida desde un puerto a partir de un instante es aritmética simple.

**La búsqueda.** Dijkstra dependiente del tiempo que minimiza **el día de llegada**, no un costo abstracto. Esperar una salida posterior nunca hace desaparecer una anterior, así que la propiedad FIFO se cumple y Dijkstra sigue siendo válido.

**Las disrupciones.** El multiplicador de cada tramo se evalúa **en el instante en que ese tramo se navegaría**, no al reservar. Una ventana que ya habrá cerrado cuando el barco llegue no cuesta nada. Verificado sobre el grafo: en el día 200 desvía Shanghai→New Jersey por Tanger Med y S6; en el día 300 abandona S4 y va a Los Angeles por Busan y S9; fuera de las ventanas vuelve a los directos.

**Las conexiones.** Un transbordo necesita margen real (`WSC_CONNECT_BUFFER_HOURS`, 8 h por defecto): una conexión que zarpa en el instante en que la carga aterriza no es alcanzable.

**En tránsito.** Mantiene el plan asignado en vez de cederlo al default, cuya reconstrucción por distancia deshace el trabajo del calendario.

## 4. Por qué debería bajar el loss *y* el ATT

El loss paga **crédito negativo** por cada período que le gana al baseline, y la asimetría favorece la velocidad: ser 20% más rápido acredita 0.250 por día, ser 20% más lento cuesta 0.167.

Una mejora de ruteo en operación normal paga en **los 72 períodos**, haya disrupción o no. Cada punto porcentual vale unos **−3.6 de crédito**. Por eso atacar la espera es distinto de atacar las disrupciones: no compite por el mismo terreno, y baja las dos métricas a la vez.

De referencia: el default acumula −0.42 de crédito y la mejor estrategia actual −8.16, que equivale a ~2% de mejora media. El margen hasta la cota física es del 15.5%.

## 5. Por qué sirve para la Ronda 3

Cero nombres de puertos, cero ids de ruta, cero fechas, y ningún guardia que exija un escenario concreto. El calendario se deriva de `Input/` y las disrupciones se leen de `context.disruption_plans` en tiempo de ejecución.

Y lo más importante: **el calendario existe en cualquier escenario**. La ventaja de conocer las salidas reales no depende de qué se rompa ni de cuándo. Una estrategia afinada contra las disrupciones de la Ronda 2 puede no transferir nada; ésta transfiere por construcción.

## 6. Cómo correrla

Colocar `chrono_strategy.py` y `user_strategy.py` en `response_strategies\`, y:

```
python run_single.py --label chrono_s2026 --seed 2026
python loss.py Output/Baseline_ATT_By_Statistics_Interval.csv Output/Analysis/att_chrono_s2026.csv
```

Referencias a batir con la semilla 2026: **loss 3.103** y **ATT 14.086**.

Antes de la corrida larga conviene la prueba de un minuto, que verifica que carga sin excepciones:

```
python run_batch.py --baseline-only --seeds 2026 --days 10 --warmup 20
```

## 7. Qué calibrar después, si funciona

| Variable | Defecto | Qué controla |
|---|---|---|
| `WSC_CONNECT_BUFFER_HOURS` | 8 | margen mínimo para dar por buena una conexión |
| `WSC_TRANSFER_PENALTY_HOURS` | 6 | castigo por transbordo más allá de su tiempo medido |
| `WSC_BERTH_CALL_DAYS` | 0.125 | tiempo en muelle por escala, que fija las fases |
| `WSC_MAX_LEGS` | 4 | tramos máximos por cadena |
| `WSC_KEEP_PLAN` | 1 | mantener el plan en tránsito |

`WSC_BERTH_CALL_DAYS` es el más sensible: entra en el cálculo de las fases, así que un valor equivocado desalinea todo el calendario. Sería el primero que yo probaría.

## 8. Riesgo honesto

El modelo del calendario es una **derivación**, no una lectura del estado real de los buques: supone reparto uniforme por el ciclo. Si el simulador coloca los buques de otra forma, las fases estarán desfasadas y el router elegirá conexiones que no existen — y entonces esto rendirá peor que el promedio de medio headway, que al menos no se equivoca sistemáticamente.

Es la apuesta principal de la propuesta y solo la corrida la resuelve. Si sale mal, el diagnóstico está en el ATT: si sube mucho, las fases están mal; si baja poco, el calendario acierta pero el margen era menor de lo estimado.
