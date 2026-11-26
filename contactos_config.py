"""
La configuración de «A quién escribirle hoy» por clínica (#558): el link de reseña de Google, los
textos de los mensajes, cada cuánto se sugiere el control, el tope del día y qué motivos sugiere el
panel. Una fila de `contact_settings` por clínica; lo que falta vale lo de fábrica.

Los textos propios llevan marcadores entre llaves que el backend completa, así la vista previa de la
configuración y el panel dicen lo mismo:

| Marcador | Qué pone |
|---|---|
| `{nombre}` | El nombre de pila de quien lee el mensaje (el paciente o el adulto a cargo) |
| `{pacientes}` | Los nombres de pila de los pacientes del aviso («Tomás y Sofía») |
| `{meses}` | Los meses sin venir (control) |
| `{fecha}` | El día del presupuesto, `dd/mm` |
| `{clinica}` | El nombre de la clínica |
| `{profesional}` | El profesional de la última atención o del presupuesto |
| `{link_resena}` | El link de reseña de Google de la clínica |

Un texto vacío es «el de fábrica», que arma `contactos_hoy.mensaje`: ése sí se acomoda a lo que
falta (sin profesional dice «Te escribimos de…», con dos chicos dice «les»). Un texto propio no: lo
que no se sabe queda vacío.
"""
import re
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from models import ContactSettings

DIAS_CONTROL = 180
DIAS_CONTROL_MINIMO = 90
DIAS_CONTROL_MAXIMO_CONFIGURABLE = 730
TOPE_DIARIO = 10
TOPE_MAXIMO = 30
LARGO_MAXIMO_TEXTO = 1000
MOTIVOS_DEL_PANEL = ("PRESUPUESTO", "CONTROL")
MARCADORES = ("nombre", "pacientes", "meses", "fecha", "clinica", "profesional", "link_resena")
TEXTOS = ("control", "chicos", "presupuesto", "resena")

# Cómo se ve lo de fábrica con todos los datos: lo que muestra la configuración para arrancar a
# cambiarlo. El mensaje de verdad lo arma `contactos_hoy.mensaje`, que dice lo mismo cuando no falta
# nada (lo cuida `test_los_textos_de_fabrica_dicen_lo_mismo_que_el_armador`).
DE_FABRICA = {
    "control": ("Hola {nombre}, ¿cómo estás? Soy {profesional}, de {clinica}. Ya pasaron {meses} meses "
                "desde tu última consulta: es un buen momento para un control. ¿Querés que te reservemos un turno?"),
    "chicos": ("Hola {nombre}, ¿cómo estás? Soy {profesional}, de {clinica}. Ya pasaron {meses} meses "
               "desde la última consulta de {pacientes}: es un buen momento para un control. "
               "¿Querés que le reservemos un turno?"),
    "presupuesto": ("Hola {nombre}, ¿cómo estás? Soy {profesional}, de {clinica}. Quería saber si te quedó "
                    "alguna duda con el presupuesto del {fecha}. Si querés, lo vemos juntos."),
    "resena": ("Hola {nombre}, ¿cómo estás? Soy {profesional}, de {clinica}. Gracias por venir. Si te "
               "quedaste conforme con la atención, nos ayudaría mucho que nos dejes una reseña en Google: "
               "{link_resena}"),
}

_MARCADOR = re.compile(r"\{([^{}]*)\}")


@dataclass
class Config:
    link_resena: Optional[str] = None
    textos: dict = field(default_factory=dict)  # sólo los propios; lo que falta es de fábrica
    dias_control: int = DIAS_CONTROL
    tope_diario: int = TOPE_DIARIO
    motivos_activos: tuple = MOTIVOS_DEL_PANEL


def leer(db: Session, clinic_id: int) -> Config:
    fila = db.get(ContactSettings, clinic_id)
    if fila is None:
        return Config()
    motivos = tuple(m for m in (fila.motivos_activos or "").split(",") if m in MOTIVOS_DEL_PANEL)
    return Config(
        link_resena=fila.link_resena or None,
        textos={t: getattr(fila, f"texto_{t}") for t in TEXTOS if getattr(fila, f"texto_{t}")},
        dias_control=fila.dias_control or DIAS_CONTROL,
        tope_diario=fila.tope_diario or TOPE_DIARIO,
        motivos_activos=motivos if fila.motivos_activos is not None else MOTIVOS_DEL_PANEL,
    )


def _volcar(fila: ContactSettings, cfg: Config) -> None:
    fila.link_resena = cfg.link_resena
    for t in TEXTOS:
        setattr(fila, f"texto_{t}", cfg.textos.get(t))
    fila.dias_control = cfg.dias_control
    fila.tope_diario = cfg.tope_diario
    fila.motivos_activos = ",".join(m for m in MOTIVOS_DEL_PANEL if m in cfg.motivos_activos)


def guardar(db: Session, clinic_id: int, cfg: Config) -> None:
    """
    Una fila por clínica. Si dos guardan a la vez la primera vez, los dos intentan crearla: el segundo
    choca con la clave primaria y entonces escribe sobre la que dejó el primero (gana el último).
    """
    fila = db.get(ContactSettings, clinic_id)
    if fila is None:
        fila = ContactSettings(clinic_id=clinic_id)
        db.add(fila)
    _volcar(fila, cfg)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        fila = db.get(ContactSettings, clinic_id)
        _volcar(fila, cfg)
        db.commit()


def es_el_de_fabrica(cual: str, texto: str) -> bool:
    """
    Un texto igual al de fábrica se guarda como «de fábrica» (vacío): si se guardara como propio, dejaría
    de acomodarse a lo que falta («Soy , de…» sin profesional, «le» con dos chicos). Pasa con «Partir
    del de fábrica» y guardar sin cambiar nada (lo encontró la revisión).
    """
    return " ".join((texto or "").split()) == " ".join(DE_FABRICA[cual].split())


def marcadores_desconocidos(texto: str) -> list:
    """Los marcadores que no existen, en el orden en que aparecen y sin repetir."""
    vistos = []
    for nombre in _MARCADOR.findall(texto or ""):
        if nombre not in MARCADORES and nombre not in vistos:
            vistos.append(nombre)
    return vistos


def link_valido(link: str) -> bool:
    """Sólo `https://`, sin espacios y con algo después: es lo que se abre en el teléfono del paciente.
    El largo (500) lo corta antes el schema (`ConfigPedido`, `VistaPreviaPedido`)."""
    return bool(re.fullmatch(r"https://[^\s<>\"']+\.[^\s<>\"']+", link or ""))


def completar(texto: str, valores: dict) -> str:
    """Pone cada marcador con su valor (vacío si no se sabe) y emprolija los espacios que quedan."""
    salida = _MARCADOR.sub(lambda m: str(valores.get(m.group(1)) or ""), texto)
    salida = re.sub(r"[ \t]{2,}", " ", salida)
    salida = re.sub(r" ([,.:;?!])", r"\1", salida)
    return salida.strip()
