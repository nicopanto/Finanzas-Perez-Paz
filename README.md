# Finanzas familiares

App local y privada para revisar los movimientos familiares desde una carpeta compartida.

## Archivos principales

- `finanzas.html`: la app web autocontenida.
- `finanzas-data.json`: la base compartida de movimientos, categorías y reglas.
- `scripts/build_initial_data.py`: regenera el JSON inicial desde `Gastos.xlsx`.
- `scripts/validate_finanzas.py`: valida importes, duplicados e importación de los archivos del banco.

## Flujo de uso

### Cómodo, con guardado automático

1. Arranca la app local:

```bash
python3 scripts/finanzas_server.py
```

2. Abre `http://127.0.0.1:8765`.
3. Descarga o reemplaza en esta carpeta los archivos `Movimientos de Cuenta*.xls`.
4. Pulsa `Actualizar desde la carpeta`.
5. Revisa solo las categorías pendientes.
6. En `Reglas`, crea o ajusta reglas para que la próxima importación clasifique mejor.

En este modo la app lee la carpeta y guarda `finanzas-data.json` automáticamente.

### Manual, abriendo el HTML directamente

1. Abre `finanzas.html` en el navegador.
2. Pulsa `Cargar JSON` y selecciona `finanzas-data.json`.
3. En `Importar`, selecciona uno o varios archivos `Movimientos de Cuenta*.xls`.
4. Revisa las categorías sugeridas en `Revisión`.
5. Pulsa `Exportar JSON` cuando quieras guardar los cambios.

El navegador descargará una nueva versión de `finanzas-data.json`; ese archivo es la base viva para la siguiente sesión.

## Validación

```bash
/Users/nicolasperezpanto/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 scripts/validate_finanzas.py
```

## Resincronizar desde Excel

Si actualizas la hoja `Data` de `Gastos.xlsx`, regenera la base compartida con:

```bash
/Users/nicolasperezpanto/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 scripts/build_initial_data.py
```

Ese proceso reemplaza el histórico del Excel, conserva los movimientos importados del banco que no estén en el Excel y reconstruye las reglas aprendidas.
