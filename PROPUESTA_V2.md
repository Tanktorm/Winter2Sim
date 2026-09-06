# Propuesta v2 — qué es alcanzable y cómo llegar

## 1. El espacio real de mejora (medido, no estimado)

| Referencia | ATT | Qué es |
|---|---|---|
| **Cota física de la red** | **11.71 d** | Navegación pura a 20 nudos, sin esperas ni atraques ni transbordos, ponderada por demanda. **Ninguna estrategia puede bajar de aquí.** |
| Sin disrupciones, default | 13.86 d | Lo que la red da cuando nada falla |
| Sin disrupciones, ruteo por tiempo esperado | 13.75 d | Ya medido: se le puede ganar al default incluso sin disrupciones |
| **Con disrupciones, default** | **15.60 d** | media de 3 semillas — **el número a batir** |

**El premio total es de 1.74 días** (15.60 − 13.86): eso es todo el daño que causan las disrupciones. Una estrategia perfecta que las anulara por completo llegaría a ~13.9. Bajar de ahí exige mejorar el ruteo en régimen normal, donde el margen adicional es de ~0.1 d.

Cualquier resultado por debajo de 11.71 es un error de medición, no una mejora. Conviene tener esta tabla a mano al comparar propuestas.

## 2. Qué ya está probado (no repetir el trabajo)

**Funciona:**
- Rutear por **tiempo esperado de tránsito** en vez de distancia. El default hace Dijkstra sobre millas náuticas y por eso reserva carga en tramos cuyo tiempo fue multiplicado por 5.
- Evaluar el multiplicador **en el instante futuro** en que el contenedor recorrería el tramo. El desvío se activa y se apaga solo, sin fechas codificadas.
- **Respetar el plan de reservas en tránsito.** Ceder esa decisión al default hace que rehaga la cadena con distancia pura y deshaga el desvío. Vale 0.57 días.
- Espera de embarque y penalización de transbordo en el costo. Quitarlas lleva el ATT a 27 días.

**No funciona:**
- **Ponderar la cola** por (ruta, punto de embarque): 15.86 con peso 0, 16.79 con 0.2, 16.26 con 1.0. Se realimenta.
- **Retener carga** hacia puertos cerrados: código muerto, el tránsito siempre dura más que el cierre.
- **Excluir las rutas alternativas** del default: sin efecto medible.
- **Replanificar con costo propio en cada llegada** (`WSC_REPLAN_MODE=own`): correcto pero inviable, más de 2 h sin terminar el warm-up.

Estado actual: **15.535 contra 15.601 del default** en 3 semillas. Empate dentro del ruido.

## 3. Las tres palancas que quedan, en orden de valor esperado

### P1 — Override selectivo (la de mejor relación valor/riesgo)

Hoy mi estrategia se hace cargo de **todas** las reservas. Pero solo gana donde hay disrupción de por medio; en el resto compite de igual a igual con el default y a veces pierde por ruido.

La corrección: calcular el camino propio y **compararlo contra el que elegiría el default**. Si ambos coinciden, o si ninguna disrupción activa o futura toca ninguno de los dos, devolver `None` y dejar que decida el default. Intervenir solo cuando el camino del default cruza un tramo o puerto afectado dentro de la ventana en que pasaría por ahí.

Por qué debería ganar: conserva íntegro el comportamiento del default donde el default es bueno, y aplica el ruteo por tiempo esperado solo donde está demostrado que gana. Convierte una apuesta global en una apuesta local.

### P2 — Calibrar los pesos (nunca se hizo)

`TRANSFER_BUFFER_HOURS`, `WAIT_WEIGHT`, `MAX_TRANSFERS` están puestos a ojo. `run_batch.py` existe para esto y no llegó a usarse: el entorno remoto se reciclaba antes de terminar la primera configuración.

Espacio sugerido: `TRANSFER_BUFFER_HOURS` 0–48 · `WAIT_WEIGHT` 0.3–1.5 · `MAX_TRANSFERS` 1–3 · `CLOSURE_SLACK_DAYS` 0–5.

Protocolo: 2–3 semillas fijas por configuración, objetivo `media + 0.5 × desviación`, y validar las mejores con semillas que el optimizador no vio. **Una configuración que gane con una sola semilla no significa nada** — lo aprendí perdiendo: con 2 semillas parecía haber una mejora de 0.20 d y la tercera invirtió el signo.

### P3 — Atacar la cola, no la media

El ATT es un promedio y esconde la distribución. Cada `Shipment` guarda `generated_time` y `completion_time`, recorribles vía `demand.shipments`, así que se puede exportar un log por envío y calcular **ATT por par origen-destino y percentiles P90/P95**.

Con eso se ve qué pares concretos se disparan durante las disrupciones. Es información que hoy no existe y que convierte "el ATT subió 1.7 días" en "estos 4 pares OD aportan el 80% del exceso". Optimizar a ciegas contra un promedio es la razón por la que las tres primeras hipótesis de esta sesión fallaron.

## 4. Protocolo de medición (esto no es opcional)

1. Reportar siempre la **media de los 72 períodos**, nunca el valor de un período suelto.
2. Mínimo **3 semillas**. Con una sola, las corridas divergen desde el día 1 y las diferencias son ruido.
3. Verificar siempre los **TEU completados**. Un ATT que baja mientras los TEU completados caen no es una mejora: es carga que dejó de moverse.
4. Comparar contra el default **en la misma semilla**, no contra un número recordado.
5. Comprobar que el resultado esté por encima de **11.71**. Si no, es un error de medición.

El punto 3 es el que más importa ahora mismo: un ATT muy bajo con carga varada en origen es exactamente lo que produce un número espectacular y falso.
