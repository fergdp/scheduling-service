"""
La base de presupuestos de «A quién escribirle hoy» sale de `QUOTING_DB_NAME` (#563).

El entorno demo tiene sus propias bases: con el nombre escrito en el código, la demo leería los
presupuestos de producción. El nombre va adentro del SQL, así que sólo se aceptan letras, dígitos y `_`.
"""
import importlib

import pytest

import contactos_hoy


def _recargar(monkeypatch, valor):
    if valor is None:
        monkeypatch.delenv("QUOTING_DB_NAME", raising=False)
    else:
        monkeypatch.setenv("QUOTING_DB_NAME", valor)
    return importlib.reload(contactos_hoy)


@pytest.fixture(autouse=True)
def _dejar_como_estaba(monkeypatch):
    yield
    monkeypatch.delenv("QUOTING_DB_NAME", raising=False)
    importlib.reload(contactos_hoy)


def test_sin_la_variable_es_la_de_produccion(monkeypatch):
    assert _recargar(monkeypatch, None).BASE_PRESUPUESTOS == "dental_quoting_db"


def test_con_la_variable_usa_esa(monkeypatch):
    assert _recargar(monkeypatch, "demo_quoting_db").BASE_PRESUPUESTOS == "demo_quoting_db"


def test_la_consulta_usa_la_base_de_la_variable(monkeypatch):
    ch = _recargar(monkeypatch, "demo_quoting_db")
    import inspect
    fuente = inspect.getsource(ch)
    # Ninguna consulta nombra la base de producción escrita a mano.
    assert "FROM dental_quoting_db" not in fuente
    assert fuente.count("{BASE_PRESUPUESTOS}.quote") >= 3


@pytest.mark.parametrize("valor", ["", "dental_quoting_db; DROP TABLE x", "base-con-guion", "a.b", "x" * 65])
def test_un_nombre_que_no_es_de_base_frena_el_arranque(monkeypatch, valor):
    with pytest.raises(ValueError):
        _recargar(monkeypatch, valor)
