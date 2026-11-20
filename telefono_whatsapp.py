"""
El número de un teléfono en el formato de `wa.me` (#556): sólo dígitos y con código de país.

⚠️ Es una COPIA de `numeroParaWhatsApp` de dental-front (`src/utils/whatsapp.js`, #298/#295). Hace
falta acá porque «A quién escribirle hoy» decide en el backend a quién mostrar: un paciente con un
teléfono que no se entiende no ocupa un lugar del tope del día, va a la lista de «teléfono mal
cargado». Si la regla cambia allá, cambia acá. Los casos de `tests/test_telefono_whatsapp.py` son
los mismos que los de `whatsapp.test.js`.

En la base los teléfonos están como los tipeó cada uno: «011 15 5555-1234», «+54 9 11 5555 1234»,
«(0351) 4-555123». Para Argentina un celular en WhatsApp es 54 + 9 + código de área SIN el 0 +
número SIN el 15, diez dígitos en total después del 549.
"""
import re
from typing import Optional

# Diez dígitos: código de área (2 a 4) + número local, sin el 0 ni el 15.
DIGITOS_NACIONALES = 10


def _sin_quince(digitos: str) -> str:
    """
    Saca el 15 de los celulares escritos a la vieja usanza («11 15 5555-1234»): quedan doce dígitos
    y el 15 va justo después del código de área. El único código de área de dos dígitos es el 11; si
    no, el 15 va después de tres o de cuatro, y las dos posiciones no pueden coincidir.
    """
    if len(digitos) != DIGITOS_NACIONALES + 2:
        return digitos
    posiciones = [2] if digitos.startswith("11") else [3, 4]
    for p in posiciones:
        if digitos[p:p + 2] == "15":
            return digitos[:p] + digitos[p + 2:]
    return digitos


def numero_para_whatsapp(telefono) -> Optional[str]:
    """
    El número para `wa.me`, o `None` si no se puede saber cuál es: mejor no mostrar el botón que
    abrir el chat de un desconocido.
    """
    if not telefono:
        return None
    crudo = str(telefono).strip()
    digitos = re.sub(r"\D", "", crudo)
    con_prefijo_internacional = crudo.startswith("+") or digitos.startswith("00")
    if digitos.startswith("00"):
        digitos = digitos[2:]

    # Ningún código de área argentino empieza con 5: un 54 adelante es el código de país.
    if digitos.startswith("54"):
        digitos = digitos[2:]
        if digitos.startswith("9"):
            digitos = digitos[1:]
    elif con_prefijo_internacional:
        # Otro país: se confía en lo cargado. Salvo diez dígitos justos, que es un número argentino
        # con un «+» de más («+351 555-1234» abría un chat de Portugal).
        if len(digitos) == DIGITOS_NACIONALES:
            return None
        return digitos if 8 <= len(digitos) <= 15 else None

    if digitos.startswith("0"):
        digitos = digitos[1:]
    digitos = _sin_quince(digitos)

    # «15 5555-1234»: el celular sin código de área. No hay forma de saber de qué ciudad es.
    if len(digitos) != DIGITOS_NACIONALES or digitos.startswith("15"):
        return None
    return f"549{digitos}"
