# ccbot — alertas de Cash Converters en Discord

Vigila búsquedas concretas de [cashconverters.es](https://www.cashconverters.es) y avisa
por un webhook de Discord cuando aparece un producto nuevo que encaja con tus filtros.
Sin servicios de pago, sin nube: se ejecuta en tu Mac cada 10 minutos con `launchd`.

## Cómo funciona

1. Descarga el HTML de cada búsqueda con `curl_cffi` imitando el fingerprint TLS de
   Chrome (la web bloquea clientes HTTP normales).
2. Extrae los productos de los *tiles* de Salesforce Commerce Cloud: cada uno es un
   elemento con `data-pid`, y ese pid (p. ej. `CC002_E882562_0`) es el identificador
   estable que se usa para deduplicar, **no la URL**.
3. Aplica tus filtros locales sobre el título (minúsculas y sin acentos).
4. Compara con `state/<busqueda>.json` y avisa por Discord solo de lo que no había visto.

## Puesta en marcha

```bash
cd ~/ccbot
python3 -m venv .venv
./.venv/bin/pip install curl_cffi beautifulsoup4 pytest
```

Pon tu webhook en `.env` (este fichero no se versiona):

```
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/XXXX/YYYY
```

Marca lo que ya existe como visto, para no recibir un aluvión en el primer aviso:

```bash
./.venv/bin/python cc_bot.py --seed
```

Y programa la ejecución automática cada 10 minutos:

```bash
cp com.usuario.ccbot.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.usuario.ccbot.plist
launchctl list | grep ccbot        # debe aparecer con estado 0
```

Para pararlo: `launchctl unload ~/Library/LaunchAgents/com.usuario.ccbot.plist`.

## Dónde se ejecuta

El bot corre en **GitHub Actions**, en un repo privado, cada 30 minutos. Así no depende
de que el iMac esté encendido y despierto. Comprobado que el WAF de Cash Converters no
bloquea las IPs de GitHub: el runner recibe el listado igual que tu Mac.

- El webhook vive en **Settings → Secrets and variables → Actions** como
  `DISCORD_WEBHOOK_URL`. Nunca en el repo.
- El estado (`state/*.json`) **sí se versiona**: es la memoria del bot entre
  ejecuciones, y el workflow lo guarda con un commit cuando cambia.
- El fichero `state/_latido.txt` cambia una vez al día para forzar un commit diario.
  Sin él, GitHub desactiva solo los workflows programados de un repo que lleve 60 días
  sin actividad.
- Si una búsqueda falla, el script termina con código 1, el workflow sale en rojo y
  GitHub te manda un correo. No hay fallos mudos.

Para lanzarlo a mano: pestaña **Actions → ccbot → Run workflow**. Ahí tienes también la
casilla `seed`, que marca todo como visto sin avisar (úsala al añadir una búsqueda).

> **Antes de tocar nada en local, haz `git pull --rebase`.** El bot hace commits desde
> la nube cada vez que cambia el estado, así que tu copia se queda atrás enseguida y el
> push te lo rechazará.

El cron de GitHub es orientativo: en el plan gratuito se retrasa a menudo 10-20 minutos.
Si algún día quieres inmediatez, el `launchd` del Mac sigue disponible (ver más abajo),
pero **no ejecutes los dos a la vez**: llevan estados separados y recibirías los avisos
por duplicado.

## Modos de ejecución

| Comando | Qué hace |
|---|---|
| `python cc_bot.py` | Ejecución normal: revisa todas las búsquedas activas y avisa de lo nuevo. |
| `python cc_bot.py --seed` | Marca todo lo actual como visto **sin avisar**. Úsalo la primera vez y al añadir una búsqueda. |
| `python cc_bot.py --dump` | Guarda el HTML crudo en `debug_<busqueda>.html` sin procesarlo. Herramienta de diagnóstico. |
| `python cc_bot.py --solo ps5-pro` | Ejecuta solo esa búsqueda. Se combina con los anteriores. |

## Añadir una búsqueda nueva

1. Haz la búsqueda en el navegador con los filtros que quieras y copia la URL.
2. Añádele `&sz=48` para que devuelva 48 resultados en vez de los 12 por defecto
   (el parámetro `sz` está verificado: `sz=96` devuelve 96 productos).
3. Añade el bloque a `config.json`:

```json
{
  "busquedas": [
    {
      "nombre": "ps5-pro",
      "url": "https://www.cashconverters.es/es/es/search/?q=ps5+pro&lang=es&sz=48",
      "activa": true,
      "keywords_todas": ["ps5"],
      "keywords_ninguna": ["mando", "funda", "cable", "soporte", "skater"],
      "precio_max": 450
    }
  ]
}
```

- `nombre`: identifica la búsqueda; da nombre a su fichero de estado y sale en el aviso
  de Discord. Sin espacios ni barras.
- `activa`: ponlo a `false` para desactivarla sin borrarla.
- `keywords_todas`: **todas** deben aparecer en el título.
- `keywords_ninguna`: si aparece **alguna**, se descarta.
- `precio_max`: descarta lo que cueste más. Si un producto no trae precio legible, se
  avisa igualmente (mejor un aviso de más que perderse una ganga).
- Los tres filtros son opcionales: omítelos y no se aplican. Se comparan sin acentos y
  en minúsculas, así que `portatil` casa con `Portátil`.

4. Lanza `python cc_bot.py --seed --solo <nombre>` para no recibir de golpe todo el
   catálogo actual de esa búsqueda. Haz lo mismo si relajas los filtros de una búsqueda
   ya existente: solo se recuerda lo que pasó el filtro, así que al ampliarlo aparecerán
   como nuevos productos que llevaban tiempo en la web.

### Cuando buscas un modelo que el buscador de la web ignora

Cash Converters, si no tiene lo que pides, te devuelve cosas parecidas sin avisar.
Buscar `macbook air m4` da **300 productos y ninguno es M4**: son MacBook Air Core i5
de 2012-2019. El buscador se traga el token `m4` y busca solo "macbook air".

Filtrar exigiendo `"keywords_todas": ["macbook air", "m4"]` funciona hoy, pero falla
**en silencio** el día que titulen la máquina sin el chip. Para un aviso que no te
quieres perder, es mejor invertirlo: pide la familia y **descarta los modelos viejos
conocidos**, que es lo que hace la búsqueda `macbook-air-m4` de `config.json`:

```json
"keywords_todas": ["macbook air"],
"keywords_ninguna": ["core i", "core 2", "m1", "m2", "m3", "funda", "cargador", "..."]
```

Así pasa cualquier MacBook Air que no sea Intel, M1, M2 ni M3. Hoy eso genera un solo
"falso positivo" (un M5 Air), que en realidad es justo lo que querrías saber. Cuando
salga el M6 y no te interese, añades `"m4"`, `"m5"` a `keywords_ninguna`.

Ojo a la interacción con `sz`: las coincidencias exactas suben a las primeras
posiciones (buscando `macbook air m1`, los 24 M1 reales ocupan los puestos 2 a 25), así
que si el título lleva el chip basta un `sz` pequeño. Pero como aquí el objetivo es
cazarlo **aunque el título no lleve el chip**, la búsqueda usa `sz=500` para barrer el
listado entero: son 2,4 MB por ejecución en vez de 1 MB. Si prefieres ahorrar tráfico y
te fías de que pondrán "m4" en el título, baja a `sz=96`.

### Búsquedas con muchos resultados

El bot solo mira los primeros `sz` resultados. Si tu búsqueda devuelve cientos de
productos, o afinas la consulta, o la ordenas por novedades añadiendo
`&srule=Novedad%20Tienda%20Desde` a la URL, para que lo recién llegado salga primero.

## Cuando dejen de llegar avisos

El síntoma en `cc_bot.log` casi siempre es este:

```
[ps5-pro] ERROR: 0 productos extraidos.
```

Cuando eso pasa, el bot **no** toca el fichero de estado a propósito: así, al arreglarlo,
no te llega el catálogo entero como si fuera nuevo. Diagnóstico, en orden:

1. **Mira el HTML que está llegando de verdad:**

   ```bash
   ./.venv/bin/python cc_bot.py --dump --solo ps5-pro
   grep -c 'data-pid=' debug_ps5-pro.html
   ```

2. **Si hay `data-pid` pero el bot extrae 0** → cambió la maquetación. Todo el scraping
   vive en la sección `2. EXTRACCION` de `cc_bot.py`, en funciones pequeñas:
   - `_desde_tiles()`: qué elemento es un producto y de dónde sale el título.
   - `_precio_de_tile()`: precio vigente (`.sales`) ignorando el tachado (`.strike-through`).
   - `_imagen_de_tile()`: imagen, incluido el *lazy load* en `data-src`.

   Abre `debug_*.html`, busca un producto, ajusta los selectores y lanza `pytest`.

3. **Si no hay ningún `data-pid` y el HTML parece una página de bloqueo o un captcha** →
   te ha detectado el WAF. Primero prueba a cambiar `impersonate="chrome"` por otra
   versión (`chrome124`, `safari17_0`) en `descargar()`. Si no basta, hay que pasar a
   Playwright: sustituye el cuerpo de `descargar()` por la carga con navegador; el resto
   del código no se entera porque toda la red pasa por esa única función.

4. **Si el título de la página dice "N productos" pero llegan menos tiles** → el `sz`
   no está funcionando; revisa que siga en la URL de `config.json`.

5. **Si el log está en silencio total**, el que no se está ejecutando es launchd:

   ```bash
   launchctl list | grep ccbot
   cat launchd.err.log
   ```

## Tests

```bash
./.venv/bin/pytest
```

No tocan la red ni Discord: usan HTML de ejemplo que imita los tiles reales. Cubren el
precio rebajado frente al tachado, los formatos de precio (`99,94 €`, `1.299,00 €`,
`549.00`, `1.299`), la deduplicación por `data-pid` repetido (pasa con los carruseles de
"productos relacionados"), las imágenes en `data-src` y con URL `//`, los filtros, y que
`--seed` no dispara notificaciones.

## Ficheros

```
cc_bot.py                 script principal
config.json               búsquedas y filtros (versionable, sin secretos)
.env                      DISCORD_WEBHOOK_URL (NO se versiona)
state/<busqueda>.json     productos ya vistos, con retención de 30 días
cc_bot.log                registro de todas las ejecuciones
test_cc_bot.py            tests
com.usuario.ccbot.plist   programación con launchd cada 10 minutos
debug_<busqueda>.html     solo lo genera --dump
```
