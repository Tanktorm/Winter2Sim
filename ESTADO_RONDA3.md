# Estado de la Ronda 3: revisión completa (20 de octubre)

Entrega: **23 de octubre**. Este documento junta lo último que sacó Alemanuel, lo que
se comparó en la sesión "Estrategia Winte2Sim optimizada", nuestros resultados y
dónde está guardado cada cosa.

## 1. Conclusión

**La mejor propuesta medida es `r3_strategy.py` versión v2** (rama
`claude/kind-cori-b7g371` de `Tanktorm/Winter2Sim`). En el banco de 7 escenarios de
Alemanuel tiene score robusto **−23.45**, contra 6.96 de su versión corregida y 8.35
de su v5. Gana en los 7 casos. Ver la tabla de la sección 3.

## 2. Una confusión de nombres que cambió la conclusión del equipo

El 19 de octubre se comparó "r3 con el trial 23" contra v5 en el banco y salió
promedio +33.30 y robusto 54.68. La conclusión fue que "la de Tanque" solo sirve en
la Ronda 2. **Esa "r3" no es nuestra estrategia.** Es `round2_strategy.py`, la
estrategia de la Ronda 2 con los parámetros del trial 23 de Optuna (estudio
`wsc_round2_loss`, loss −4.43 en la Ronda 2). Esa versión tiene el guardia
`is_round_two_context`, que solo se activa con las disrupciones exactas de la Ronda 2.
En cualquier otro escenario devuelve `None` y juega el default. Por eso esa columna
coincide con el default: 83.55 en el cierre de hub y 15.61 en la red renombrada.

Nuestro `r3_strategy.py` no tiene guardia ni nombres. Medido en los mismos 7
escenarios (definiciones copiadas de `hidden_scenarios.py`, verificadas idénticas):

## 3. Banco de Alemanuel, semilla 2026 (loss, menor es mejor)

| Caso | "r3" de AleM = default | v5 (trial 28) | Corregida (Codex) | Nuestro r3 | **Nuestro v2** |
|---|---|---|---|---|---|
| 01 desvíos oceánicos | 46.74 | −18.17 | −17.39 | −30.30 | **−42.52** |
| 02 cierre de hub | 83.55 | 16.12 | 23.35 | 11.27 | **−12.24** |
| 03 flota escasa, demanda alta | 17.42 | −30.76 | −26.57 | −59.64 | **−93.90** |
| 05 disrupciones en un servicio | 40.15 | −19.03 | −17.81 | −37.60 | **−44.71** |
| 06 topología distinta | 29.72 | 10.49 | 10.95 | −0.86 | **−10.63** |
| 07 red renombrada y lenta | 15.61 | 15.61 | −18.21 | −44.42 | **−58.02** |
| 08 disrupciones rápidas | −0.11 | 7.40 | 0.53 | −20.64 | **−21.35** |
| **Promedio** | 33.30 | −2.62 | −6.45 | −26.03 | **−40.48** |
| **Score robusto** | 54.68 | 8.35 | 6.96 | −8.68 | **−23.45** |

Fórmula del score robusto, la misma de Alemanuel: promedio + 0.35·desviación +
0.25·(peor − promedio).

Las columnas de AleM y Codex son las que ellos reportaron. Las nuestras salen de
simulaciones completas (140 días de calentamiento + 360 medidos) con los mismos
escenarios, y son reproducibles: la misma corrida en la PC de Andrés da exactamente
el mismo número.

Otros escenarios medidos con v2:

| Escenario | r3 | v2 |
|---|---|---|
| Ronda 2 oficial, semilla 2026 | −15.94 | **−21.35** |
| Ronda 2, semilla 2027 | −16.43 | **−27.60** |
| Ronda 2, semilla 2028 | −16.86 | **−23.04** |
| h3: cierre de 40 días de Tanger Med | 48.05 | **40.54** (default: 123.13) |

## 4. Lo que reportó Codex sobre la versión corregida de Alemanuel

- La estrategia ya no apaga el ruteo ni la prioridad de atraque cuando no detecta un
  desvío rentable (antes caía al default). Eso explica el salto del caso 07, de 15.61
  a −18.21.
- Permite asignar ruta aunque el destino esté cerrado (antes retenía la carga).
- Score robusto 6.96 contra 8.35 de v5, pero v5 sigue ganando en 5 de 7 casos.
- El costo de cómputo casi se duplicó (35–44 min por escenario contra 16–21), y solo
  se probó una semilla.

## 5. Dónde está guardado cada cosa

| Qué | Dónde | ¿En GitHub? |
|---|---|---|
| Estrategia v2, banco, guías, resultados de la nube, reporte | `Tanktorm/Winter2Sim`, rama `claude/kind-cori-b7g371` (PR #3) | Sí |
| Chrono v2 (calendario de salidas) | `Tanktorm/Winter2Sim`, rama `propuesta/calendario-real-v2` | Sí |
| v5 y optimizador de Alemanuel (hasta el 17-oct) | `alemanuel18/SimulationChallenge2026_Py_Round2`, rama `propuesta/alemanuel1_Round3` | Sí |
| Versión corregida de Alemanuel (reporte Codex) | Solo en la máquina de Alemanuel | **No** |
| Rama `ronda3` en la PC de Andrés | Solo local | **No**, hasta hacer push |
| Corridas de la semilla 2027 de la Ronda 2 (`att_base_s2027.csv`, `att_estela_s2027.csv`) | Solo en la PC de Andrés | **No** |
| Lo único de la Ronda 2 que sí se subió | push del 6-sep (`f06b8ca`, rama `mejora/estela-3.10` en el repo de Alemanuel) | Sí |

Git no guarda nada solo: lo que no se hace `push` existe únicamente en esa máquina.

## 6. Cómo subir a GitHub los avances de tu PC

En VS Code, un comando a la vez. Se sube a **tu** repositorio (`tanktorm`), sin
tocar el del equipo:

```powershell
git switch ronda3
```
```powershell
git status
```
```powershell
git add response_strategies r3_bench.py RONDA3.md resultados_nube Output/R3/results.csv
```
```powershell
git commit -m "Ronda 3: avances locales"
```
```powershell
git push tanktorm ronda3
```

Para respaldar también las curvas sueltas de la semilla 2027 (4 horas de cómputo
cada una):

```powershell
git add Output/Analysis/att_base_s2027.csv Output/Analysis/att_estela_s2027.csv
```
```powershell
git commit -m "Curvas de la semilla 2027 de la Ronda 2"
```
```powershell
git push tanktorm ronda3
```

Si `git status` muestra archivos que no quieres subir (logs, carpetas de respaldo),
no los incluyas en el `git add`: solo se sube lo que nombras.

## 7. Qué entregar

`response_strategies/` con `__init__.py`, `default_strategy.py` y
`strategy_validation.py` sin cambios, más `user_strategy.py` y `r3_strategy.py` (v2)
de la rama `claude/kind-cori-b7g371`. Antes de entregar, una corrida con
`main.py` para confirmar que todo funciona como lo harán los jueces.
