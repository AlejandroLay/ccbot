"""
Tests de cc_bot.py. No tocan la red ni Discord: todo se hace con HTML de
ejemplo que imita los tiles SFRA de cashconverters.es.

    ./.venv/bin/pytest
"""

import json

import pytest

import cc_bot


# ---------------------------------------------------------------------------
# Fixtures de HTML: imitan la maquetacion real del listado
# ---------------------------------------------------------------------------

TILE_CON_REBAJA = """
<div class="product" data-pid="12345">
  <div class="image-container">
    <img class="tile-image" data-src="//img.cashconverters.es/12345.jpg" alt="Consola PS5 Pro">
  </div>
  <div class="pdp-link"><a href="/es/es/producto/consola-ps5-pro/12345.html">Consola PS5 Pro 2TB</a></div>
  <div class="price">
    <span class="strike-through list"><span class="value" content="128.95">Antes 128,95 €</span></span>
    <span class="sales"><span class="value" content="99.94">99,94 €</span></span>
  </div>
</div>
"""

TILE_DUPLICADO = """
<div class="product carousel-item" data-pid="12345">
  <div class="pdp-link"><a href="/es/es/producto/consola-ps5-pro/12345.html">Consola PS5 Pro 2TB</a></div>
  <div class="price"><span class="sales"><span class="value" content="99.94">99,94 €</span></span></div>
</div>
"""

TILE_IMG_SRC_ABSOLUTA = """
<div class="product" data-pid="67890">
  <img class="tile-image" src="https://img.cashconverters.es/67890.jpg" alt="Mando PS5">
  <div class="pdp-link"><a href="/es/es/producto/mando-ps5/67890.html">Mando DualSense PS5</a></div>
  <div class="price"><span class="sales"><span class="value" content="49.00">49,00 €</span></span></div>
</div>
"""

TILE_SIN_TITULO = """
<div class="product placeholder" data-pid="99999">
  <a href="javascript:void(0)"></a>
</div>
"""


def _pagina(*tiles):
    return "<html><body><div class='product-grid'>" + "".join(tiles) + "</div></body></html>"


# ---------------------------------------------------------------------------
# Precio: .sales gana al .strike-through
# ---------------------------------------------------------------------------

def test_precio_usa_sales_y_no_el_tachado():
    items = cc_bot.extraer_productos(_pagina(TILE_CON_REBAJA))
    assert len(items) == 1
    assert items[0]["precio"] == 99.94


def test_precio_sin_atributo_content_se_lee_del_texto():
    tile = """
    <div class="product" data-pid="555">
      <div class="pdp-link"><a href="/p/555.html">Portatil Lenovo</a></div>
      <div class="price">
        <span class="strike-through"><span class="value">1.499,00 €</span></span>
        <span class="sales"><span class="value">1.299,00 €</span></span>
      </div>
    </div>
    """
    items = cc_bot.extraer_productos(_pagina(tile))
    assert items[0]["precio"] == 1299.00


@pytest.mark.parametrize("texto,esperado", [
    ("99,94 €", 99.94),
    ("1.299,00 €", 1299.00),
    ("549.00", 549.00),
    ("1.299", 1299.0),
    ("1.299.000", 1299000.0),
    ("1,299.00", 1299.00),
    (329, 329.0),
    (None, None),
    ("", None),
    ("sin precio", None),
])
def test_parseo_de_precios(texto, esperado):
    assert cc_bot._a_float(texto) == esperado


# ---------------------------------------------------------------------------
# Deduplicacion por data-pid
# ---------------------------------------------------------------------------

def test_deduplica_el_mismo_pid_repetido():
    html = _pagina(TILE_CON_REBAJA, TILE_IMG_SRC_ABSOLUTA, TILE_DUPLICADO)
    items = cc_bot.extraer_productos(html)
    assert [i["id"] for i in items] == ["12345", "67890"]


def test_ignora_tiles_sin_titulo():
    items = cc_bot.extraer_productos(_pagina(TILE_SIN_TITULO, TILE_CON_REBAJA))
    assert [i["id"] for i in items] == ["12345"]


# ---------------------------------------------------------------------------
# Imagenes y URLs
# ---------------------------------------------------------------------------

def test_imagen_lazy_load_en_data_src_y_url_con_doble_barra():
    items = cc_bot.extraer_productos(_pagina(TILE_CON_REBAJA))
    assert items[0]["imagen"] == "https://img.cashconverters.es/12345.jpg"


def test_imagen_con_src_absoluta_se_respeta():
    items = cc_bot.extraer_productos(_pagina(TILE_IMG_SRC_ABSOLUTA))
    assert items[0]["imagen"] == "https://img.cashconverters.es/67890.jpg"


def test_url_relativa_se_convierte_en_absoluta():
    items = cc_bot.extraer_productos(_pagina(TILE_CON_REBAJA))
    assert items[0]["url"] == "https://www.cashconverters.es/es/es/producto/consola-ps5-pro/12345.html"


# ---------------------------------------------------------------------------
# Filtros
# ---------------------------------------------------------------------------

def _item(titulo, precio=100.0):
    return {"id": "1", "titulo": titulo, "precio": precio, "url": "u", "imagen": None}


def test_keywords_todas_deben_aparecer_todas():
    busqueda = {"keywords_todas": ["ps5", "pro"]}
    assert cc_bot.pasa_filtros(_item("Consola PS5 Pro 2TB"), busqueda)
    assert not cc_bot.pasa_filtros(_item("Consola PS5 Slim"), busqueda)


def test_keywords_ninguna_descarta():
    busqueda = {"keywords_ninguna": ["mando", "funda"]}
    assert not cc_bot.pasa_filtros(_item("Mando DualSense PS5"), busqueda)
    assert cc_bot.pasa_filtros(_item("Consola PS5 Pro"), busqueda)


def test_precio_max_descarta_lo_caro():
    busqueda = {"precio_max": 450}
    assert cc_bot.pasa_filtros(_item("Consola PS5 Pro", 449.99), busqueda)
    assert not cc_bot.pasa_filtros(_item("Consola PS5 Pro", 599.0), busqueda)
    # sin precio detectado no se descarta: mejor avisar de mas que perder una ganga
    assert cc_bot.pasa_filtros(_item("Consola PS5 Pro", None), busqueda)


def test_filtros_ignoran_acentos_y_mayusculas():
    busqueda = {"keywords_todas": ["portatil"]}
    assert cc_bot.pasa_filtros(_item("PORTÁTIL Lenovo IdeaPad"), busqueda)
    busqueda = {"keywords_ninguna": ["cámara"]}
    assert not cc_bot.pasa_filtros(_item("Camara Sony Alpha"), busqueda)


def test_busqueda_sin_filtros_lo_acepta_todo():
    assert cc_bot.pasa_filtros(_item("Cualquier cosa"), {})


# Titulos reales del catalogo. El buscador de la web ignora el token "m4" y
# devuelve 300 MacBook antiguos, asi que el criterio real es "MacBook Air que
# no sea de los chips viejos conocidos": si algun dia titulan el M4 sin poner
# el chip, el aviso llega igual (mejor un falso positivo que un fallo mudo).
BUSQUEDA_M4 = {
    "keywords_todas": ["macbook air"],
    "keywords_ninguna": ["core i", "core 2", "m1", "m2", "m3",
                         "funda", "cargador", "cable", "adaptador",
                         "teclado", "carcasa", "bateria", "magic", "raton"],
}


@pytest.mark.parametrize("titulo", [
    "portatil apple apple macbook air m4 10-core 4.0 13 (2025) (a3240)",
    "portatil apple apple macbook air m5 15 (10gpu) 16gb 512gb (a3448)",
    "portatil apple apple macbook air 13 (2025) (a3240) 16gb 256gb",  # sin chip en el titulo
])
def test_avisa_de_los_macbook_air_modernos(titulo):
    assert cc_bot.pasa_filtros(_item(titulo, 1100.0), BUSQUEDA_M4)


@pytest.mark.parametrize("titulo", [
    "portatil apple apple macbook air core i5 1.8 13 (2017) (a1466)",
    "portatil apple apple macbook air m1 8-core 3.2/8 13 (2020) (a2337)",
    "portatil apple apple macbook air m2 8-core 3.4 13 (8gpu) (2022) (a2681)",
    "portatil apple apple macbook pro m3 8-core 4.0 14 (10gpu) (2023)",
    "portatil apple apple macbook pro core i7 2.6 15 touchbar (2019) (a1990)",
    "funda macbook air 13",
    "cargador apple macbook air usb-c 30w",
])
def test_descarta_el_ruido_de_la_busqueda_de_macbook(titulo):
    assert not cc_bot.pasa_filtros(_item(titulo, 300.0), BUSQUEDA_M4)


# ---------------------------------------------------------------------------
# Estado y modos de ejecucion (sin red, sin Discord)
# ---------------------------------------------------------------------------

@pytest.fixture
def entorno(tmp_path, monkeypatch):
    """Redirige rutas y descarga a un entorno de pruebas aislado."""
    monkeypatch.setattr(cc_bot, "BASE_DIR", tmp_path)
    monkeypatch.setattr(cc_bot, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(cc_bot, "LOG_FILE", tmp_path / "cc_bot.log")
    monkeypatch.setattr(cc_bot, "descargar", lambda url, intentos=3: _pagina(TILE_CON_REBAJA, TILE_IMG_SRC_ABSOLUTA))

    avisos = []
    monkeypatch.setattr(cc_bot, "avisar", lambda nuevos, url, nombre: avisos.append((nombre, list(nuevos))))
    return tmp_path, avisos


BUSQUEDA = {"nombre": "prueba", "url": "http://ejemplo", "activa": True}


def test_seed_no_dispara_notificaciones(entorno):
    tmp_path, avisos = entorno
    cc_bot.procesar_busqueda(BUSQUEDA, "http://webhook", modo_dump=False, modo_seed=True)

    assert avisos == []
    estado = json.loads((tmp_path / "state" / "prueba.json").read_text())
    assert set(estado) == {"12345", "67890"}


def test_tras_seed_una_ejecucion_normal_no_avisa_de_nada(entorno):
    tmp_path, avisos = entorno
    cc_bot.procesar_busqueda(BUSQUEDA, "http://webhook", modo_dump=False, modo_seed=True)
    cc_bot.procesar_busqueda(BUSQUEDA, "http://webhook", modo_dump=False, modo_seed=False)
    assert avisos == []


def test_ejecucion_normal_avisa_de_los_productos_no_vistos(entorno):
    tmp_path, avisos = entorno
    cc_bot.procesar_busqueda(BUSQUEDA, "http://webhook", modo_dump=False, modo_seed=False)

    assert len(avisos) == 1
    nombre, nuevos = avisos[0]
    assert nombre == "prueba"
    assert {i["id"] for i in nuevos} == {"12345", "67890"}


def test_cero_productos_no_pisa_el_estado_ni_avisa(entorno, monkeypatch):
    tmp_path, avisos = entorno
    cc_bot.procesar_busqueda(BUSQUEDA, "http://webhook", modo_dump=False, modo_seed=True)
    estado_previo = (tmp_path / "state" / "prueba.json").read_text()

    # simula bloqueo del WAF: la pagina llega sin productos
    monkeypatch.setattr(cc_bot, "descargar", lambda url, intentos=3: "<html><body>Bot detection</body></html>")
    cc_bot.procesar_busqueda(BUSQUEDA, "http://webhook", modo_dump=False, modo_seed=False)

    assert avisos == []
    assert (tmp_path / "state" / "prueba.json").read_text() == estado_previo


def test_busqueda_inactiva_se_omite(entorno):
    tmp_path, avisos = entorno
    inactiva = dict(BUSQUEDA, activa=False)
    cc_bot.procesar_busqueda(inactiva, "http://webhook", modo_dump=False, modo_seed=False)

    assert avisos == []
    assert not (tmp_path / "state" / "prueba.json").exists()


def test_dump_guarda_html_y_no_toca_el_estado(entorno):
    tmp_path, avisos = entorno
    cc_bot.procesar_busqueda(BUSQUEDA, "http://webhook", modo_dump=True, modo_seed=False)

    assert (tmp_path / "debug_prueba.html").exists()
    assert avisos == []
    assert not (tmp_path / "state" / "prueba.json").exists()


def test_estado_olvida_lo_mas_viejo_de_30_dias(entorno):
    from datetime import datetime, timedelta, timezone
    tmp_path, _ = entorno
    viejo = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    reciente = datetime.now(timezone.utc).isoformat()

    cc_bot.guardar_vistos("prueba", {"viejo": viejo, "reciente": reciente})
    estado = cc_bot.cargar_vistos("prueba")

    assert set(estado) == {"reciente"}


def test_estado_corrupto_no_revienta(entorno):
    tmp_path, _ = entorno
    (tmp_path / "state").mkdir(exist_ok=True)
    (tmp_path / "state" / "prueba.json").write_text("{no es json")
    assert cc_bot.cargar_vistos("prueba") == {}


# ---------------------------------------------------------------------------
# .env
# ---------------------------------------------------------------------------

def test_lectura_de_env(tmp_path):
    env = tmp_path / ".env"
    env.write_text('# comentario\nDISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/1/2"\nVACIA=\n')
    valores = cc_bot.cargar_env(env)
    assert valores["DISCORD_WEBHOOK_URL"] == "https://discord.com/api/webhooks/1/2"
    assert valores["VACIA"] == ""


def test_env_inexistente_devuelve_vacio(tmp_path):
    assert cc_bot.cargar_env(tmp_path / "no-existe") == {}


def test_el_webhook_de_entorno_manda_sobre_el_fichero(tmp_path, monkeypatch):
    """En GitHub Actions el webhook llega por variable de entorno desde Secrets."""
    env = tmp_path / ".env"
    env.write_text("DISCORD_WEBHOOK_URL=https://del-fichero\n")
    monkeypatch.setattr(cc_bot, "ENV_FILE", env)

    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://del-entorno")
    assert cc_bot.obtener_webhook() == "https://del-entorno"

    monkeypatch.delenv("DISCORD_WEBHOOK_URL")
    assert cc_bot.obtener_webhook() == "https://del-fichero"


def test_sin_webhook_en_ningun_sitio_devuelve_vacio(tmp_path, monkeypatch):
    monkeypatch.setattr(cc_bot, "ENV_FILE", tmp_path / "no-existe")
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    assert cc_bot.obtener_webhook() == ""


# ---------------------------------------------------------------------------
# Codigo de salida: una busqueda rota tiene que hacer fallar la ejecucion,
# para que GitHub Actions la marque en rojo y avise por correo.
# ---------------------------------------------------------------------------

def test_busqueda_correcta_devuelve_ok(entorno):
    assert cc_bot.procesar_busqueda(BUSQUEDA, "http://webhook", False, True) is True


def test_cero_productos_devuelve_fallo(entorno, monkeypatch):
    monkeypatch.setattr(cc_bot, "descargar", lambda url, intentos=3: "<html>Bot detection</html>")
    assert cc_bot.procesar_busqueda(BUSQUEDA, "http://webhook", False, False) is False


def test_fallo_de_descarga_devuelve_fallo(entorno, monkeypatch):
    def peta(url, intentos=3):
        raise ConnectionError("sin red")
    monkeypatch.setattr(cc_bot, "descargar", peta)
    assert cc_bot.procesar_busqueda(BUSQUEDA, "http://webhook", False, False) is False


def test_hay_nuevos_pero_falta_webhook_devuelve_fallo(entorno):
    assert cc_bot.procesar_busqueda(BUSQUEDA, "", False, False) is False
