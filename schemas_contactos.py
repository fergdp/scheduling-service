"""
La forma de la API de «A quién escribirle hoy» (#556). Las reglas viven en `contactos_hoy.py`.

⚠️ La respuesta no trae nada clínico: ni qué se le hizo al paciente, ni montos, ni ítems de un
presupuesto. La recepción la lee. Un campo nuevo se anota también en
`tests/test_contactos_api.py::CAMPOS_PERMITIDOS`, a propósito: ese test falla con cualquier campo
que no esté en la lista.
"""
from datetime import date
from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator

from schemas import ContactoParaEscribir

Motivo = Literal["CONTROL", "PRESUPUESTO", "RESENA"]
Accion = Literal["ESCRITO", "POSPUESTO", "NO_SUGERIR", "NO_SUGERIR_NADA"]


class PacienteDelAviso(BaseModel):
    patient_user_id: int
    nombre: Optional[str] = None
    # Sólo en PRESUPUESTO: el presupuesto de este paciente, para marcarlo.
    quote_id: Optional[int] = None


class Aviso(BaseModel):
    """
    Un mensaje para mandar: a un número, por un motivo. Puede nombrar a varios pacientes (los
    hermanos que van al mismo tutor). `clave` identifica el aviso en la pantalla.
    """
    clave: str
    motivo: Literal["CONTROL", "PRESUPUESTO"]
    # Un control que va al tutor: la pantalla lo muestra como «Chicos».
    es_chicos: bool
    pacientes: list[PacienteDelAviso]
    contacto: ContactoParaEscribir
    # El número ya normalizado para `wa.me`: sólo dígitos y con código de país.
    numero_whatsapp: str
    razon: str
    dias: int
    fecha_presupuesto: Optional[date] = None
    presupuesto_vencido: bool = False
    profesional_user_id: Optional[int] = None
    profesional: Optional[str] = None
    mensaje: str


class SinTelefono(BaseModel):
    """Un paciente al que le tocaba un aviso y no tiene un teléfono que se entienda: hay que corregirlo."""
    patient_user_id: int
    nombre: Optional[str] = None
    motivo: Literal["CONTROL", "PRESUPUESTO"]


class AvisosDelDia(BaseModel):
    """
    `hoy` es el día de Córdoba contra el que se hicieron las cuentas: una pantalla abierta desde
    ayer tiene que poder darse cuenta, como en «Controles a agendar» (#354).
    """
    hoy: date
    tope: int
    avisos: list[Aviso]
    sin_telefono: list[SinTelefono]
    # Los motivos que salieron vacíos porque no se pudo leer algo: la pantalla lo avisa.
    motivos_con_error: list[Literal["CONTROL", "PRESUPUESTO"]]


class PacienteAMarcar(BaseModel):
    patient_user_id: int = Field(gt=0)
    quote_id: Optional[int] = Field(default=None, gt=0)


class MarcaPedido(BaseModel):
    """
    Lo que hizo la persona con un aviso. Varios pacientes porque un aviso puede nombrar a varios.

    - `ESCRITO` con `RESENA` es un pedido de reseña (el botón manual).
    - `POSPUESTO` lleva `hasta` (por defecto, de acá a 7 días).
    - `PRESUPUESTO` lleva el `quote_id` de cada paciente.
    """
    motivo: Motivo
    accion: Accion
    pacientes: list[PacienteAMarcar] = Field(min_length=1, max_length=20)
    hasta: Optional[date] = None

    @model_validator(mode="after")
    def _coherente(self):
        if self.motivo == "RESENA" and self.accion != "ESCRITO":
            raise ValueError("Una reseña sólo se marca como pedida (ESCRITO)")
        if self.accion != "POSPUESTO" and self.hasta is not None:
            raise ValueError("«hasta» va sólo con POSPUESTO")
        con_presupuesto = [p.quote_id is not None for p in self.pacientes]
        if self.motivo == "PRESUPUESTO" and self.accion != "NO_SUGERIR_NADA" and not all(con_presupuesto):
            raise ValueError("Un presupuesto se marca con el quote_id de cada paciente")
        if self.motivo != "PRESUPUESTO" and any(con_presupuesto):
            raise ValueError("quote_id va sólo con PRESUPUESTO")
        ids = [p.patient_user_id for p in self.pacientes]
        if len(set(ids)) != len(ids):
            raise ValueError("Un paciente repetido")
        return self


class MarcaGuardada(BaseModel):
    mark_id: int
    patient_user_id: int
    motivo: Motivo
    accion: Accion
    quote_id: Optional[int] = None
    fecha: date
    hasta: Optional[date] = None


class MarcasGuardadas(BaseModel):
    marcas: list[MarcaGuardada]




# ---------------------------------------------------------------------------
# Reseñas y configuración (#558)
# ---------------------------------------------------------------------------

class ResenaDelPaciente(BaseModel):
    """
    Lo del botón «Pedir reseña» de un paciente: cuántas veces se pidió y, si la clínica tiene el
    link configurado, a qué número va y el mensaje ya armado (a quién va lo decide #417).
    """
    veces: int
    ultima: Optional[date] = None
    numero_whatsapp: Optional[str] = None
    contacto: Optional[ContactoParaEscribir] = None
    mensaje: Optional[str] = None


class Resenas(BaseModel):
    """`link_configurado` en falso: el botón no se ofrece, y quien edita ve dónde cargarlo."""
    link_configurado: bool
    pacientes: dict[int, ResenaDelPaciente]


class TextosDeContactos(BaseModel):
    control: Optional[str] = None
    chicos: Optional[str] = None
    presupuesto: Optional[str] = None
    resena: Optional[str] = None


class ConfigDeContactos(BaseModel):
    """
    La configuración de la clínica. En `textos`, `null` es «el de fábrica», que viene en
    `textos_de_fabrica` para mostrarlo y arrancar a cambiarlo. `puede_editar`: el admin y el dueño
    de la clínica (como la configuración de Cobros, #371); el resto la lee.
    """
    link_resena: Optional[str] = None
    textos: TextosDeContactos
    textos_de_fabrica: TextosDeContactos
    marcadores: list[str]
    dias_control: int
    tope_diario: int
    motivos_activos: list[Literal["PRESUPUESTO", "CONTROL"]]
    puede_editar: bool


class ConfigPedido(BaseModel):
    """
    Lo que se guarda, entero: lo que no viene vuelve a lo de fábrica. Un texto vacío (o sólo
    espacios) también es «el de fábrica». Las reglas que miran el contenido (marcadores, el link)
    las aplica el router, para poder decir cuál es el que falla.
    """
    link_resena: Optional[str] = Field(default=None, max_length=500)
    textos: TextosDeContactos = Field(default_factory=TextosDeContactos)
    dias_control: int = Field(default=180, ge=90, le=730)
    tope_diario: int = Field(default=10, ge=1, le=30)
    motivos_activos: list[Literal["PRESUPUESTO", "CONTROL"]] = Field(default_factory=lambda: ["PRESUPUESTO", "CONTROL"])


class VistaPreviaPedido(BaseModel):
    """Un texto (vacío = el de fábrica) armado con un paciente de ejemplo, para la configuración."""
    cual: Literal["control", "chicos", "presupuesto", "resena"]
    texto: Optional[str] = Field(default=None, max_length=1000)
    link_resena: Optional[str] = Field(default=None, max_length=500)


class VistaPrevia(BaseModel):
    mensaje: str


# ---------------------------------------------------------------------------
# Métricas (#559): sólo cuentas, ningún paciente.
# ---------------------------------------------------------------------------

class MetricaDelMotivo(BaseModel):
    """
    Las marcas de un motivo en el período. `funcionaron`: volvió (control) o aceptó (presupuesto)
    dentro de los 30 días; `en_curso`: no todavía, y le quedan días. `cumplidos`: a las que ya se
    les pasaron los 30 días, y `funcionaron_cumplidos`, las que funcionaron entre ésas: el
    porcentaje va con estos dos. Los cuatro son `null` en RESENA (no se sabe) y cuando no se pudo
    leer con qué se cruzan (`motivos_con_error`).
    """
    escritos: int
    funcionaron: Optional[int] = None
    en_curso: Optional[int] = None
    cumplidos: Optional[int] = None
    funcionaron_cumplidos: Optional[int] = None
    pospuestos: int
    descartados: int


class SemanaDeMetricas(BaseModel):
    """Los «le escribí» de una semana, de lunes a domingo, por motivo."""
    lunes: date
    control: int
    presupuesto: int
    resena: int


class MetricasDeContactos(BaseModel):
    desde: date
    hasta: date
    ventana_dias: int
    control: MetricaDelMotivo
    presupuesto: MetricaDelMotivo
    resena: MetricaDelMotivo
    semanas: list[SemanaDeMetricas]
    motivos_con_error: list[str]
