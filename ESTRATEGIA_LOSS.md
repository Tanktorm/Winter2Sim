# Estrategia orientada al Loss (métrica oficial)

## 1. Qué premia realmente la métrica

```
loss = Σ sobre los 72 períodos de  (1 − ATT_baseline / ATT_corrida) × días_del_período
```

Tres consecuencias que cambian el diseño:

**Un período más rápido que el baseline resta loss.** No es solo "aguantar" las disrupciones: ir más rápido que el baseline en cualquier período genera **crédito negativo**. El baseline se calcula con la estrategia por defecto y sin disrupciones, así que cualquier mejora de ruteo en operación normal paga.

**La métrica es asimétrica y favorece la velocidad.**

| Desviación | Costo por día |
|---|---|
| 20% más lento | +0.167 |
| 20% más rápido | **−0.250** |

Ser rápido acredita más de lo que penaliza ser lento en la misma proporción. La métrica premia agresividad, no prudencia.

**Todos los períodos valen igual, haya o no disrupción.** Los ~250 días sin disrupción pesan tanto como los ~110 con ella.

## 2. De dónde viene el loss de cada estrategia (medido)

| Corrida | loss | penalización | crédito |
|---|---|---|---|
| Default | 34.57 | 35.00 | −0.42 |
| Estrategia de esta rama | 40.30 | 42.54 | −2.24 |
| **Alemanuel** | **3.30** | 11.45 | **−8.16** |

Y separando por zona del año:

| Zona | Default | Alemanuel |
|---|---|---|
| Ventanas de disrupción | 21.55 | **−3.80** |
| Estela (30 días después) | 10.24 | **5.78** |
| Períodos tranquilos | 2.79 | **1.31** |

Esto dice exactamente dónde está el trabajo que queda:

- **Alemanuel ya resolvió las ventanas de disrupción.** Ahí no solo no pierde: acredita −3.80. Atacar ese frente es redundante.
- **Su loss restante es 5.78 de estela + 1.31 de períodos tranquilos.** La estela es el 78% de lo que le queda.
- **En los períodos tranquilos es 1.31 peor que el baseline.** Ahí no hay disrupción que lo justifique: es su ruteo normal rindiendo por debajo del default.

## 3. Las dos palancas, con su techo cuantificado

### L1 — La estela de las disrupciones (5.78 disponibles)

La carga que se reserva **durante** una disrupción completa 20–40 días después, ya con la red sana. Sus períodos 286–315 son seis consecutivos con ATT ~18.5 contra un baseline de ~14.2, y caen justo después del cierre de un puerto.

El mecanismo probable: cuando un puerto está cerrado, el costo de todo camino que lo toca se infla con la espera completa hasta la reapertura, lo que empuja la carga a desvíos mucho más largos. Esos desvíos se pagan enteros aunque el puerto reabra a los pocos días.

La corrección es evaluar el cierre **en el instante estimado de llegada**, no en el de la reserva, y comparar explícitamente dos opciones: esperar la reapertura contra tomar el desvío. Hoy solo se considera la segunda.

### L2 — Los períodos tranquilos (el techo grande, sin explotar)

El baseline da 13.86 días. La **cota física de la red** —navegación pura a 20 nudos, sin esperas ni atraques ni transbordos, ponderada por demanda— es **11.71 días**. Entre ambos hay 2.15 días, un 15.5%.

Cada punto porcentual de mejora sostenida en operación normal vale aproximadamente **−3.6 de crédito** repartido en los 72 períodos. Llevar el ruteo normal a un 10% mejor que el baseline valdría del orden de −40, un orden de magnitud más que todo el loss actual de cualquiera.

Nadie está explotando esto. Alemanuel saca −8.16 de crédito, que corresponde a ~2% de mejora media. El default saca −0.42. Es el terreno con más margen y el menos disputado, **y además es el que mejor se transfiere a la Ronda 3**: mejorar el ruteo en condiciones normales no depende de qué disrupciones aparezcan.

## 4. Diseño para la Ronda 3 (evaluación a ciegas)

La solución se entrega antes de conocer el escenario. Todo lo que dependa del escenario actual es deuda.

**Prohibido:** nombres de puertos o rutas en el código, días concretos, constantes por ruta (`lead_margin_s5`, `enable_s7_skip`), y guardias que exijan un conjunto exacto de disrupciones para activarse. Un guardia así convierte toda la estrategia en `return None` cuando cambia un puerto.

**En su lugar:** parámetros en función de **propiedades** de la ruta, no de su identidad — su headway, su número de buques, la severidad del tramo afectado, cuántas rutas alternativas sirven ese puerto. Mismo poder expresivo, y se transfiere.

**Al inicio de cada corrida**, calcular la topología: qué rutas sirven cada puerto, qué puertos son hoja (una sola ruta), qué headway tiene cada servicio, y qué pares origen-destino no tienen alternativa. Eso se deriva de `Input/` y vale para cualquier escenario.

**En cada decisión**, leer las disrupciones activas y futuras de `context.disruption_plans` y evaluar su efecto en el instante en que la carga pasaría por ahí. Ese mecanismo ya funciona y es completamente general.

**Flota atrapada.** Un cierre puede partir la red en dos. Si demasiados buques quedan del lado equivocado, el otro lado se queda sin servicio y el ATT se dispara en pares que ni siquiera tocaban la disrupción. Vigilar la distribución de la flota por región y evitar que una decisión de desvío concentre buques a un lado. Esto no aparece en el ATT promedio hasta que ya es tarde.

**Degradación segura.** Ante cualquier situación no prevista, devolver `None` y dejar actuar al default. Una estrategia que falla en un escenario desconocido puntúa peor que no tener estrategia.

## 5. Protocolo de experimentación

**Puntuar siempre con el loss**, nunca con el ATT promedio. Son métricas distintas y he comprobado que ordenan las estrategias de forma diferente: por ATT mi estrategia parecía empatar con el default; por loss está claramente peor (40.3 contra 34.6).

**Descomponer cada corrida** en penalización, crédito y zona (disrupción / estela / tranquilo). Un loss total no dice dónde intervenir; esa descomposición sí, y es lo que localizó las dos palancas de arriba.

**Tres semillas mínimo.** Con dos semillas mi estrategia parecía mejorar 0.20 días de forma consistente; la tercera invirtió el signo. Una diferencia menor que la dispersión entre semillas no existe.

**Verificar los TEU completados en cada corrida.** Un loss que mejora mientras la carga completada cae no es una mejora: es carga que dejó de moverse.

**Volumen.** El equipo anterior llegó donde llegó con dos meses de experimentos, no con una idea brillante. Una estrategia en la que confiaban les dio 153. La intuición no predice el resultado en un sistema con esta cantidad de realimentaciones — de mis cuatro hipótesis en esta sesión, tres resultaron falsas y solo se supo midiendo. Repartir configuraciones entre varias máquinas y registrar cada corrida en una tabla común.
