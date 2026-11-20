"""
La copia de `numeroParaWhatsApp` (#556). ⚠️ Los casos son los de dental-front,
`src/utils/__tests__/whatsapp.test.js`: si allá se suma uno, se suma acá.
"""
import pytest

from telefono_whatsapp import numero_para_whatsapp


@pytest.mark.parametrize("telefono, esperado", [
    ("11 5555-1234", "5491155551234"),
    ("1155551234", "5491155551234"),
    ("011 15 5555-1234", "5491155551234"),
    ("11 15 5555 1234", "5491155551234"),
    ("+54 9 11 5555-1234", "5491155551234"),
    ("+54 11 5555 1234", "5491155551234"),
    ("54 9 11 5555 1234", "5491155551234"),
    ("0054 9 11 5555 1234", "5491155551234"),
    ("0351 15 555-1234", "5493515551234"),
    ("(0351) 4-555123", "5493514555123"),
    ("2226 15 55-1234", "5492226551234"),
    ("+54 9 2226 15 55 1234", "5492226551234"),
])
def test_telefonos_argentinos_como_se_cargan_a_mano(telefono, esperado):
    assert numero_para_whatsapp(telefono) == esperado


def test_un_numero_de_otro_pais_se_respeta_con_prefijo_internacional():
    assert numero_para_whatsapp("+598 99 123 456") == "59899123456"
    assert numero_para_whatsapp("00598 99 123 456") == "59899123456"


def test_diez_digitos_con_mas_son_un_numero_argentino_mal_escrito():
    assert numero_para_whatsapp("+351 555-1234") is None
    assert numero_para_whatsapp("+11 5555-1234") is None
    assert numero_para_whatsapp("0011 5555 1234") is None
    assert numero_para_whatsapp("+54 11 5555-1234") == "5491155551234"
    assert numero_para_whatsapp("+1 23") is None


@pytest.mark.parametrize("telefono", ["15 5555-1234", "4555-1234", "11 5555 1234 99", "no tiene", "", None])
def test_lo_que_no_se_entiende_no_se_inventa(telefono):
    assert numero_para_whatsapp(telefono) is None


def test_lo_ya_normalizado_queda_igual():
    """El front vuelve a normalizar lo que le manda el backend: tiene que dar lo mismo."""
    assert numero_para_whatsapp("5493515551234") == "5493515551234"
