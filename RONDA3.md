# Ronda 3 (a ciegas): estrategia y tutorial

Entrega: **23 de octubre de 2026**. Escenario oculto: pueden cambiar las disrupciones,
la red, los puertos, los tramos, las rutas, la flota y la demanda.

## 0. Estado actual: versión v3 (20 de octubre)

`r3_strategy.py` incluye el **espaciado de buques**. El simulador suelta los buques de
cada ruta cada 7 días sin importar la duración del ciclo, lo que deja salidas
desparejas (en R2 hay huecos de 0.4 a 7 días en la misma ruta). Un buque que va pegado
al anterior toma un camino un poco más largo hecho de tramos existentes hasta que los
huecos se emparejan. Así la carga espera menos en origen y en los transbordos.

**Qué cambió en v3.** En v2 el peor caso era h3 (cierre de 40 días de Tanger Med):
loss +40.54. La causa: durante el cierre los buques esperan en el fondeadero y al
reabrir salen todos juntos. En Rotterdam, 7 de los 8 buques de S1 salían en menos de
un día; el primero llevaba 2,595 TEU y los otros entre 6 y 387. v2 solo podía mover
cada buque 2.5 días cada medio ciclo, así que el grupo seguía junto el resto del año.
v3 hace dos cambios:

1. **Buques casi vacíos.** En el período medido, un buque que sale con a lo sumo la
   mitad de la carga que suele llevar desde esa escala se espacia con los límites del
   calentamiento (sin el tope de 2.5 días ni la espera de medio ciclo). Retrasarlo
   cuesta poco porque lleva poca carga (`phase_light_ratio = 0.5`).
2. **Vuelve a espaciar antes** después de un cierre o una congestión: 0.25 ciclos en
   vez de 1 (`phase_guard_before_cycles = 0.25`).

Medido en la nube con el mismo simulador (resultados idénticos a tu PC):

| Escenario | `r3` original | v2 | **v3** | Alemanuel v6 (trial 9) |
|---|---|---|---|---|
| Ronda 2, semilla 2026 | −15.94 | −21.35 | **−28.61** | – |
| Ronda 2, semilla 2027 | −16.43 | −27.60 | **−32.90** | – |
| Ronda 2, semilla 2028 | −16.86 | −23.04 | **−34.41** | – |
| a1 tramos oceánicos | −30.30 | −42.52 | **−48.56** | −19.37 |
| a2 cierre de Singapore | 11.27 | −12.24 | **−35.26** | 24.95 |
| a3 flota escasa, demanda alta | −59.64 | −93.90 | **−108.86** | −25.40 |
| a5 eventos solapados | −37.60 | −44.71 | **−58.59** | −13.64 |
| a6 sin ruta S4 | −0.86 | −10.63 | **−26.62** | 4.80 |
| a7 red renombrada y lenta | −44.42 | −58.02 | **−62.73** | −19.11 |
| a8 choques cortos | −20.64 | −21.35 | **−29.22** | −10.08 |
| h3 estrés (cierre de 40 días) | 48.05 | 40.54 | **−4.27** | – |
| **Score robusto (7 casos de Alemanuel)** | −8.68 | −23.45 | **−37.08** | 5.71 |

v3 gana en los 11 casos (promedio −42.73 contra −28.62 de v2) y completa más carga en todos. Para comparar con v2: `--set phase_light_ratio=0 --set phase_guard_before_cycles=1`.

## 1. La decisión

La entrega de la Ronda 2 **no sirve** para la Ronda 3: tiene un guardia
(`is_round_two_context`) que exige las disrupciones exactas de la Ronda 2 y parámetros
por nombre de ruta (`s4`, `s5`, …). En el escenario oculto el guardia falla y las
1,079 líneas se vuelven un `return None`: compite la estrategia por defecto.

Esta rama trae una estrategia nueva, `response_strategies/r3_strategy.py`, construida
para cualquier red:

- **Cero nombres de puertos, ids de ruta o fechas.** Todo se deriva del contexto.
- **Lee todas las disrupciones por adelantado** desde `context.disruption_plans`,
  con la regla exacta del simulador (chequeo diario; si dos planes tocan el mismo
  tramo o muelle, gana el último de la lista).
- **Cierre parcial ≠ cierre total.** Si cierran 1 de 3 muelles, el puerto sigue
  operando y la estrategia no lo evita (el default sí lo evita).
- **Degradación segura.** Cualquier error interno apaga solo ese componente y
  devuelve la decisión al default. La corrida nunca se cae.

## 2. Qué hace (y de dónde viene cada pieza)

| Componente | Qué hace | Evidencia |
|---|---|---|
| Ruteo por tiempo | Dijkstra que minimiza la **hora de llegada**: espera, escalas, tramo congestionado con el multiplicador del día en que se navegaría, puerto cerrado = esperar a que reabra | Mismo principio que la estrategia del equipo en R2 (junto con el desvío de S5): loss 34.6 del default → ~3 |
| Desvío de flota completa | Si navegar el tramo congestionado tarda más que rodearlo con tramos existentes (+1 día de margen), **todos** los buques de esa ruta toman el desvío mientras dura el evento | La regla general reproduce lo que Optuna eligió a mano en R2: S5 sí, S4 sí, S9 no |
| Seguimiento en vivo (`live`) | Registra cada llegada y salida de cada buque, proyecta sus próximas escalas y reserva en el buque que **de verdad sale primero**, en vez de suponer una espera media | Nuevo. En pruebas unitarias (sin simulación completa) predice la siguiente escala con 0.07 días de error medio. Se apaga solo si en la corrida el error medio pasa de 1.5 días |
| Replanificación | Al llegar un buque a puerto, se reprograma la carga a bordo (y, en modo vivo, la que espera en ese puerto) **solo si** el plan nuevo llega al menos 1 día antes (2 días sin modo vivo) | Corrige el defecto de la R2: devolver `None` dejaba al default rehacer los planes por distancia |
| Prioridad de muelle | Bajo congestión, primero el buque con más carga envejecida | Igual que la entrega R2 (peso calibrado 3612) |
| Espaciado de buques (`phase`) | Empareja los huecos entre salidas de cada ruta desviando un poco al buque que va pegado al anterior (tramos existentes) | Medido: mejora los 11 escenarios probados (sección 0) |

Por qué el modo en vivo es la apuesta: la métrica da **crédito** por cada período más
rápido que el baseline, y el baseline es el default sin disrupciones. Los buques se
liberan cada 7 días y luego derivan con el ruido de ±5%: **no** están repartidos de
forma uniforme, así que la espera real difiere mucho del promedio. Conocerla vale en
los 72 períodos, haya disrupción o no, y no depende de qué se rompa en el escenario
oculto.

Riesgo honesto: el modo en vivo **no se ha medido en una simulación real**. Por eso el
plan de pruebas compara siempre `r3` (vivo) contra `r3_live0` (sin vivo) y la
decisión final sale de esa comparación.

## 3. Archivos

| Archivo | Para qué | ¿Se entrega? |
|---|---|---|
| `response_strategies/r3_strategy.py` | La estrategia | **Sí** |
| `response_strategies/user_strategy.py` | Delega en `r3_strategy` | **Sí** |
| `r3_bench.py` | Banco de pruebas: escenarios tipo "oculto", baseline propio por escenario y semilla, loss oficial | No (solo pruebas) |
| `RONDA3.md` | Esta guía | No |

## 4. Tutorial en VS Code (Windows)

> **Pega los comandos de uno en uno** y espera a que termine cada uno. Pegar varios
> juntos ya cortó una corrida de 3 horas.

### 4.1 Traer los archivos a tu proyecto

1. Abre VS Code → `File > Open Folder…` →
   `C:\Users\Andres Ortiz\Documents\winter\SimulationChallenge2026_Py_Round2`.
2. Abre la terminal: `Terminal > New Terminal` (es PowerShell).
3. Crea una rama propia para la Ronda 3, así la entrega de la Ronda 2 queda intacta:

   ```powershell
   git switch -c ronda3
   ```

4. Agrega tu repositorio como segundo remoto (solo la primera vez):

   ```powershell
   git remote add tanktorm https://github.com/Tanktorm/Winter2Sim.git
   ```

5. Descarga la rama:

   ```powershell
   git fetch tanktorm claude/kind-cori-b7g371
   ```

6. Copia los cuatro archivos a tu carpeta:

   ```powershell
   git checkout tanktorm/claude/kind-cori-b7g371 -- response_strategies/r3_strategy.py response_strategies/user_strategy.py r3_bench.py RONDA3.md
   ```

7. Confirma que llegaron:

   ```powershell
   git status
   ```

   Debes ver `r3_strategy.py`, `user_strategy.py`, `r3_bench.py` y `RONDA3.md`.

Si `git fetch` pide usuario, inicia sesión con tu cuenta de GitHub (Tanktorm). Si
prefieres no usar git: en GitHub abre la rama `claude/kind-cori-b7g371` de
`Tanktorm/Winter2Sim`, descarga esos cuatro archivos y ponlos en las mismas rutas.

### 4.2 Prueba de humo (minutos)

Verifica que todo carga y corre, y te dice la velocidad de tu máquina:

```powershell
.\.venv\Scripts\python.exe r3_bench.py smoke
```

Debe terminar con `PRUEBA DE HUMO OK`. Corre 75 días simulados con la estrategia y
75 con el baseline. Anota los minutos que imprime la línea `[smoke_r3] ... listo en X
min`: **una corrida completa tarda ≈ X × 6.7**.

Si sale `PRUEBA DE HUMO FALLO` o aparece una línea `[R3] component ... disabled`,
cópiame el error completo y no sigas.

### 4.3 Plan rápido: Ronda 2, semilla 2026 (3 corridas)

```powershell
.\.venv\Scripts\python.exe r3_bench.py plan rapido
```

Corre el baseline (rápido: memoriza las rutas del default, mismo resultado), `r3` y
`r3_live0`. Si lo cortas con `Ctrl+C`, al relanzar sigue donde quedó (las corridas
terminadas no se repiten). Al terminar:

```powershell
.\.venv\Scripts\python.exe r3_bench.py report
```

Referencias en esta misma semilla: default **34.574**, entrega R2 **0.614**
(sobreajustada a esta semilla, no transfiere).

### 4.4 Generalidad y semillas (lo que decide)

```powershell
.\.venv\Scripts\python.exe r3_bench.py plan generalidad
```

```powershell
.\.venv\Scripts\python.exe r3_bench.py plan semillas
```

`generalidad` corre tres escenarios que **no** son la Ronda 2:

| Escenario | Qué prueba |
|---|---|
| `h1` | Misma red, otras disrupciones: tramo troncal compartido por 3 rutas, cierre del hub Singapore, cierre de Rotterdam, dos tramos congestionados más |
| `h2` | Red distinta: otra flota (S1 7, S5 8, S6 4, S9 6), Singapore con 2 muelles, demanda ×1.3 |
| `h3` | Estrés: cierre largo (40 días) de Tanger Med, cierre parcial de 1 muelle, dos eventos solapados en S5, disrupción que empieza en el warm-up |

`semillas` repite la Ronda 2 con las semillas 2027 y 2028.

Son 15 corridas en total. Si la máquina de Alemanuel es más rápida (según su log de
Optuna, una corrida le tomaba unos 15 min), córranlas ahí; con 4 núcleos o más se puede usar
`--workers 2`. En tu máquina (2 núcleos, 8 GB) deja una a la vez.

Opcional, como referencia (lentas, usan el default sin memorizar):

```powershell
.\.venv\Scripts\python.exe r3_bench.py plan default
```

### 4.5 Una corrida suelta con otro parámetro

```powershell
.\.venv\Scripts\python.exe r3_bench.py run h2 r3 --seed 2026 --set detours=0 --label r3_det0
```

Cualquier clave de `DEFAULTS` en `r3_strategy.py` se puede cambiar así sin tocar el
código.

## 5. Cómo decidir

`report` imprime el loss de cada corrida contra el baseline de su mismo escenario y
semilla, y el promedio por variante.

1. **Descalificantes** (antes de mirar el loss):
   - Aparece `[R3] component ... disabled` o `live predictions disabled` en la consola.
   - La variante completa más de 1% menos TEU (columna `TEU compl.` del report) que la
     otra variante en el mismo escenario y semilla: carga que dejó de moverse.
2. **Vivo o no vivo:** se elige la variante con **menor loss promedio** sobre las 6
   combinaciones (r2 × 3 semillas, h1, h2, h3). Si la diferencia promedio es menor
   de 1 punto, gana `r3_live0` (más simple, menos riesgo).
3. Ninguna decisión sale de una sola semilla: el loss varía ~6 puntos entre semillas.

Si gana `r3_live0`, cambia en `response_strategies/r3_strategy.py`:

```python
    "live": 1.0,
```

por

```python
    "live": 0.0,
```

Los jueces no ponen variables de entorno: lo que vale es lo que está en `DEFAULTS`.

## 6. Preparar la entrega

1. Fija los valores ganadores en `DEFAULTS` (paso 5).
2. Corrida final como la harán los jueces, para ver el dashboard:

   ```powershell
   .\.venv\Scripts\python.exe main.py
   ```

   El dashboard muestra el *Cumulative Resilience Loss* de la Ronda 2.
3. La carpeta `response_strategies/` que se entrega lleva `__init__.py`,
   `default_strategy.py`, `strategy_validation.py` (sin cambios), `user_strategy.py` y
   `r3_strategy.py`. Lo de la Ronda 2 (`round2_strategy.py`, `optimize_round2.py`,
   `optuna_state/`) no se usa: mejor no incluirlo.
4. No se tocó nada fuera de `response_strategies/`.

## 7. Registro de desarrollo

Los organizadores piden historial verificable. En tu rama `ronda3`, después de cada
paso:

```powershell
git add response_strategies Output/R3/results.csv
```

```powershell
git commit -m "Ronda 3: resultados plan rapido"
```

Guarda `Output/R3/` completo: son las curvas de cada prueba. Si preguntan cómo se
desarrolló, la respuesta tiene que ser la verdadera, incluido el uso de asistentes de
IA.

## 8. Comparar contra el banco de Alemanuel (7 casos)

`r3_bench.py` incluye los 7 casos de su optimizador (`a1`, `a2`, `a3`, `a5`, `a6`,
`a7`, `a8`), copiados de su `hidden_scenarios.py` (verificado: generan exactamente
los mismos contextos). El baseline es el default sin disrupciones, igual que en su
banco, así que los loss son directamente comparables con su trial 28.

```powershell
.\.venv\Scripts\python.exe r3_bench.py plan alemanuel --workers 2
```

```powershell
.\.venv\Scripts\python.exe r3_bench.py report
```

El `report` agrega una tabla con los 7 casos, el promedio, el peor caso y el
**score robusto** con su misma fórmula (promedio + 0.35·desviación + 0.25·(peor −
promedio)), y pone al lado los números de su trial 28 (`alemanuel_t28`: robusto
8.35, promedio −2.62, peor 16.12). Menor es mejor.

Son 14 corridas (7 baselines rápidos + 7 de `r3`). En una máquina con 7 núcleos
usar `--workers 7`; en la tuya (2 núcleos), `--workers 2`.
