# Cómo abrir y correr esto en una computadora nueva (Windows)

## 1. Abrir el proyecto en VS Code

En VS Code: **File → Open Folder…** y elegir la carpeta `SimulationChallenge2026_Py_Round2` (la que contiene `main.py`). No abrir un archivo suelto: tiene que ser la carpeta, porque el proyecto importa módulos por ruta relativa.

Si aún no está en la computadora:

```powershell
git clone https://github.com/Tanktorm/Winter2Sim.git
cd Winter2Sim
git checkout claude/winte2sim-strategy-tytkek
```

## 2. Preparar el entorno (una sola vez)

Abrir la terminal integrada con **Ctrl + Ñ** (o Terminal → New Terminal) y ejecutar:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Si PowerShell bloquea el script de activación:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

Cuando el entorno está activo, el prompt empieza con `(.venv)`. En VS Code conviene además elegirlo como intérprete: **Ctrl + Shift + P → Python: Select Interpreter → .venv**.

## 3. Verificar que todo funciona (corrida corta, ~1 minuto)

```powershell
python run_batch.py --baseline-only --seeds 2026 --days 10 --warmup 20
```

Si imprime una línea `trial 1 [default] ATT ...`, está todo bien. Si falla aquí, no tiene sentido seguir.

## 4. Una corrida normal con dashboard

```powershell
python main.py
```

Tarda bastante (140 días de warm-up + 360 medidos), escribe los CSV en `Output/`, el log en `Logs/` y abre el dashboard en http://127.0.0.1:8000/dashboard/.

## 5. Correr en automático varias horas

Esto es lo que deja la computadora trabajando sola: prueba muchas combinaciones de los pesos de la estrategia, con varias semillas cada una, y va escribiendo los resultados.

```powershell
python run_batch.py --hours 8 --workers 3
```

Qué hace:

- Empieza con los valores por defecto, para tener la referencia.
- Después va probando combinaciones al azar del espacio de búsqueda.
- Cada combinación se evalúa con **3 semillas** (2026, 2027, 2028) y se queda con `media + 0.5 × desviación`. Eso premia las configuraciones estables, no las que tuvieron suerte con una semilla.
- Escribe cada resultado en `Output/Batch/results.csv` **apenas termina**. Si se corta la luz o se cierra la terminal, lo ya calculado no se pierde.
- Se detiene sola al agotar el presupuesto de horas.

Opciones útiles:

| Opción | Para qué |
|---|---|
| `--hours 8` | presupuesto de tiempo (se detiene sola) |
| `--workers 3` | corridas en paralelo. Usar CPUs físicas menos una |
| `--days 180` | corridas más cortas: el doble de configuraciones probadas en el mismo tiempo |
| `--seeds 2026 2027` | menos semillas = más rápido, pero más ruido |
| `--trials 40` | detenerse tras N configuraciones en vez de por tiempo |
| `--report` | ver el ranking de lo ya calculado, sin correr nada |

**Estrategia recomendada para una noche de 8 horas:**

```powershell
# Exploración: corridas cortas, muchas configuraciones
python run_batch.py --hours 5 --workers 3 --days 180 --seeds 2026 2027

# Al día siguiente, ver qué zona ganó
python run_batch.py --report

# Confirmación: las mejores, a corrida completa y 3 semillas
python run_batch.py --hours 3 --workers 3 --seeds 2026 2027 2028
```

### Para que la computadora no se duerma

Windows suspende la máquina y mata la corrida. Antes de dejarla toda la noche:

```powershell
powercfg /change standby-timeout-ac 0
powercfg /change monitor-timeout-ac 10
```

(Reactivar después con `powercfg /change standby-timeout-ac 30`.)

### Para cerrar la sesión y que siga corriendo

```powershell
Start-Process -WindowStyle Hidden powershell -ArgumentList '-Command',"cd '$PWD'; .\.venv\Scripts\Activate.ps1; python run_batch.py --hours 8 --workers 3 *> batch.log"
```

El avance queda en `batch.log` y los resultados en `Output/Batch/results.csv`.

## 6. Leer los resultados

```powershell
python run_batch.py --report
```

Muestra las 15 mejores configuraciones ordenadas por objetivo. Las columnas:

- `att_mean` — el ATT promedio de las semillas. **Este es el número que se reporta.**
- `att_stdev` — cuánto varía entre semillas. **Si dos configuraciones difieren menos que esto, no son distinguibles: la diferencia es ruido.**
- `att_worst` — la peor semilla. Una configuración con buena media y mal peor-caso es frágil.
- `objective` — `att_mean + 0.5 × att_stdev`, que es lo que se minimiza.

Referencias contra las que comparar (escenario de Ronda 2, corridas ya en el repo):

| Referencia | ATT |
|---|---|
| Baseline sin disrupción | 13.85 d |
| Disrupción con la estrategia por defecto del simulador | 15.53 d |

## 7. Congelar el ganador para la entrega

Los pesos se leen de variables de entorno para poder calibrarlos. Para entregar, hay que fijarlos como constantes en `response_strategies/adaptive_strategy.py`: en la clase `Params`, cambiar el segundo argumento de cada `_env_float` / `_env_int` por el valor ganador. Se entrega la estrategia calibrada, no el buscador.

## 8. Si algo falla

| Síntoma | Causa |
|---|---|
| `No module named 'o2des'` | falta `pip install -r requirements.txt`, o el venv no está activo |
| `No module named 'loguru'` | lo mismo |
| El prompt no dice `(.venv)` | el entorno no está activo: `.\.venv\Scripts\Activate.ps1` |
| PowerShell no deja activar | `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` |
| La corrida usa toda la RAM | bajar `--workers` |
| `python` no se reconoce | instalar Python desde python.org marcando "Add python.exe to PATH" |
