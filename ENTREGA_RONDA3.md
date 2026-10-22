# Entrega de la Ronda 3

Esta rama es `main` (el código oficial) más los dos únicos archivos que se entregan:

| Archivo | Qué es |
|---|---|
| `response_strategies/user_strategy.py` | El punto de entrada que llama el simulador. Delega en `r3_strategy`. |
| `response_strategies/r3_strategy.py` | La estrategia (versión v3). |

`__init__.py`, `default_strategy.py` y `strategy_validation.py` quedan como en el
original, y nada fuera de `response_strategies/` cambia.

Resultados: v3 le gana a v2 en las 15 combinaciones escenario-semilla medidas
(promedio de loss −39.43 contra −24.00). El detalle está en la rama
`claude/kind-cori-b7g371` (`ESTADO_RONDA3.md` y `resultados_nube/`).

Antes de enviar, correr `python main.py` una vez con esta rama.
