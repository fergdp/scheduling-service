"""
«A quién escribirle hoy» (#556, épica #555): la lista del día y las marcas. Las reglas viven en
`contactos_hoy.py`; acá están los permisos y la forma de la API.

Los endpoints son `def` y no `async def`, como en la lista de espera: SQLAlchemy es sincrónico y una
consulta lenta adentro de un `async def` frena el worker entero.
"""
import logging
from datetime import timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import contactos_config
import contactos_hoy
from dependencies import get_clinic_id, get_db, get_roles, get_user_id, require_any_role
from models import ContactMark
from schemas_contactos import (
    Aviso, AvisosDelDia, ConfigDeContactos, ConfigPedido, MarcaGuardada, MarcaPedido, MarcasGuardadas,
    PacienteDelAviso, ResenaDelPaciente, Resenas, SinTelefono, TextosDeContactos, VistaPrevia,
    VistaPreviaPedido,
)

logger = logging.getLogger(__name__)

# ⚠️ Guard de rol en el ROUTER (#261): un PACIENTE también trae `clinic_id` en su JWT, y esto lista
# nombres y teléfonos de otros pacientes. La recepción entra: es quien manda los mensajes, y la
# respuesta no trae nada clínico (decisión de Fernando, 2026-11-20). `get_clinic_id` va primero:
# sin sesión, 401.
router = APIRouter(dependencies=[
    Depends(get_clinic_id),
    require_any_role("ADMIN", "RECEPTIONIST", "DENTIST"),
])

_NO_ENCONTRADO = "Paciente no encontrado"
_NO_SE_PUDO_VERIFICAR = "No se pudo verificar el paciente: probá de nuevo en un rato"
MAX_PACIENTES_RESENAS = 200


@router.get("/hoy", response_model=AvisosDelDia)
def avisos_de_hoy(
    dentist_id: Optional[int] = Query(default=None, gt=0),
    clinic_id: int = Depends(get_clinic_id),
    db: Session = Depends(get_db),
):
    """
    Los avisos del día de la clínica. Con `dentist_id`, sólo los de sus pacientes: los de su última
    atención (control) o los de sus presupuestos.
    """
    cfg = contactos_config.leer(db, clinic_id)
    dia = contactos_hoy.armar_el_dia(db, clinic_id, dentist_id=dentist_id, cfg=cfg)
    avisos = []
    for a in dia.avisos:
        primero = a.pacientes[0][0]
        presupuesto = primero.presupuesto
        es_tutor = a.contacto["es_tutor"]
        nombres = [contactos_hoy.primer_nombre(nombre) for _, nombre in a.pacientes]
        profesional = dia.profesionales.get(a.profesional_user_id)
        dias = min(c.dias for c, _ in a.pacientes)
        # Hermanos con presupuesto: con que uno esté vencido, el mensaje ofrece actualizarlos.
        vencido = any(
            c.presupuesto and c.presupuesto.fecha_vencimiento and c.presupuesto.fecha_vencimiento < dia.hoy
            for c, _ in a.pacientes
        )
        avisos.append(Aviso(
            clave=f"{a.motivo}:{a.numero}",
            motivo=a.motivo,
            es_chicos=a.motivo == "CONTROL" and es_tutor,
            pacientes=[
                PacienteDelAviso(
                    patient_user_id=c.patient_user_id, nombre=nombre,
                    quote_id=c.presupuesto.quote_id if c.presupuesto else None,
                )
                for c, nombre in a.pacientes
            ],
            contacto=a.contacto,
            numero_whatsapp=a.numero,
            razon=contactos_hoy.razon(a),
            dias=dias,
            fecha_presupuesto=presupuesto.fecha_emision if presupuesto else None,
            presupuesto_vencido=vencido,
            profesional_user_id=a.profesional_user_id,
            profesional=profesional,
            mensaje=contactos_hoy.armar_mensaje(
                cfg, a.motivo, a.contacto["nombre"], es_tutor, nombres, profesional, dia.clinica,
                meses=dias // 30,
                fecha_presupuesto=presupuesto.fecha_emision if presupuesto else None,
                presupuesto_vencido=vencido,
            ),
        ))
    # Para medir (#559): cuántos se mostraron, por motivo. Sin nombres ni teléfonos.
    logger.info(
        "contactos_hoy clinic_id=%s avisos=%s presupuesto=%s control=%s sin_telefono=%s errores=%s",
        clinic_id, len(avisos), sum(a.motivo == "PRESUPUESTO" for a in avisos),
        sum(a.motivo == "CONTROL" for a in avisos), len(dia.sin_telefono), ",".join(dia.motivos_con_error),
    )
    return AvisosDelDia(
        hoy=dia.hoy,
        tope=cfg.tope_diario,
        avisos=avisos,
        sin_telefono=[
            SinTelefono(patient_user_id=c.patient_user_id, nombre=nombre, motivo=c.motivo)
            for c, nombre in dia.sin_telefono
        ],
        motivos_con_error=dia.motivos_con_error,
    )


@router.post("/marcas", response_model=MarcasGuardadas, status_code=201)
def marcar(
    pedido: MarcaPedido,
    clinic_id: int = Depends(get_clinic_id),
    user_id: int = Depends(get_user_id),
    db: Session = Depends(get_db),
):
    """
    Anota lo que hizo la persona: le escribió (o le pidió la reseña), lo pospuso, o que no se le
    sugiera más. Cada paciente tiene que ser de la clínica (si no, 404, igual que uno que no existe),
    y cada presupuesto, de ese paciente.
    """
    hoy = contactos_hoy.hoy_en_cordoba()
    hasta = None
    if pedido.accion == "POSPUESTO":
        hasta = pedido.hasta or hoy + timedelta(days=contactos_hoy.POSPONER_DIAS)
        if not (hoy < hasta <= hoy + timedelta(days=contactos_hoy.POSPONER_DIAS_MAXIMO)):
            raise HTTPException(
                status_code=422,
                detail=f"Se pospone de mañana a {contactos_hoy.POSPONER_DIAS_MAXIMO} días como mucho",
            )

    ids = {p.patient_user_id for p in pedido.pacientes}
    try:
        pacientes = contactos_hoy.leer_pacientes(ids, clinic_id)
        presupuestos = contactos_hoy.leer_duenos_de_presupuestos(
            {p.quote_id for p in pedido.pacientes if p.quote_id is not None}, clinic_id,
        )
    except contactos_hoy._LecturaFallida:
        raise HTTPException(status_code=503, detail=_NO_SE_PUDO_VERIFICAR)
    if ids - set(pacientes):
        raise HTTPException(status_code=404, detail=_NO_ENCONTRADO)
    for p in pedido.pacientes:
        if p.quote_id is not None and presupuestos.get(p.quote_id) != p.patient_user_id:
            raise HTTPException(status_code=404, detail="Presupuesto no encontrado")

    # Un doble toque («Pedir reseña» dos veces) no anota dos: la marca igual de hoy que vale se reusa.
    iguales = db.query(ContactMark).filter(
        ContactMark.clinic_id == clinic_id, ContactMark.patient_user_id.in_(ids),
        ContactMark.motivo == pedido.motivo, ContactMark.accion == pedido.accion, ContactMark.fecha == hoy,
    ).all()
    anuladas = {
        fila[0] for fila in db.query(ContactMark.anula_a_id).filter(
            ContactMark.anula_a_id.in_([m.mark_id for m in iguales]))
    } if iguales else set()
    vigentes = {(m.patient_user_id, m.quote_id): m for m in iguales if m.mark_id not in anuladas}
    filas, nuevas = [], []
    for p in pedido.pacientes:
        fila = vigentes.get((p.patient_user_id, p.quote_id))
        if fila is None:
            fila = ContactMark(
                clinic_id=clinic_id, patient_user_id=p.patient_user_id, motivo=pedido.motivo,
                quote_id=p.quote_id, accion=pedido.accion, fecha=hoy, hasta=hasta, marked_by_user_id=user_id,
            )
            nuevas.append(fila)
        filas.append(fila)
    db.add_all(nuevas)
    db.commit()
    logger.info("contactos_hoy marca clinic_id=%s motivo=%s accion=%s pacientes=%s nuevas=%s user_id=%s",
                clinic_id, pedido.motivo, pedido.accion, len(filas), len(nuevas), user_id)
    return MarcasGuardadas(marcas=[
        MarcaGuardada(mark_id=f.mark_id, patient_user_id=f.patient_user_id, motivo=f.motivo,
                      accion=f.accion, quote_id=f.quote_id, fecha=f.fecha, hasta=f.hasta)
        for f in filas
    ])


@router.delete("/marcas/{mark_id}", status_code=204)
def deshacer(
    mark_id: int,
    clinic_id: int = Depends(get_clinic_id),
    user_id: int = Depends(get_user_id),
    db: Session = Depends(get_db),
):
    """
    Deshace una marca: anota otra, `DESHECHO`, que la anula. Deshacer dos veces es lo mismo que
    una (204): la segunda choca con el UNIQUE de `anula_a_id`. Una marca de otra clínica, 404.
    """
    marca = db.query(ContactMark).filter(
        ContactMark.mark_id == mark_id, ContactMark.clinic_id == clinic_id,
        ContactMark.accion != "DESHECHO",
    ).first()
    if marca is None:
        raise HTTPException(status_code=404, detail="Marca no encontrada")
    db.add(ContactMark(
        clinic_id=clinic_id, patient_user_id=marca.patient_user_id, motivo=marca.motivo,
        quote_id=marca.quote_id, accion="DESHECHO", fecha=contactos_hoy.hoy_en_cordoba(),
        anula_a_id=mark_id, marked_by_user_id=user_id,
    ))
    try:
        db.commit()
    except IntegrityError:
        # Ya estaba deshecha (un segundo toque, o dos a la vez): choca con el UNIQUE de `anula_a_id`.
        db.rollback()
    return Response(status_code=204)


@router.get("/resenas", response_model=Resenas)
def pedidos_de_resena(
    patient_ids: str = Query(..., description="Ids separados por coma"),
    clinic_id: int = Depends(get_clinic_id),
    db: Session = Depends(get_db),
):
    """
    Lo del botón «Pedir reseña» de cada paciente (#558), en un pedido para toda la página: cuántas
    veces se le pidió y cuándo fue la última, y —si la clínica tiene el link de reseña— a qué
    número va y el mensaje ya armado. El mensaje viene acá y no al tocar el botón porque abrir
    WhatsApp después de esperar una respuesta lo frena el bloqueador de ventanas del navegador.

    Sólo cuenta las marcas de la clínica; un id sin pedidos sale con 0, y uno de otra clínica (o
    dado de baja) sale sin mensaje. Si los pacientes no se pueden leer, salen los contadores sin
    mensaje: el botón no se ofrece, y nada más se rompe.
    """
    try:
        ids = sorted({int(x) for x in patient_ids.split(",") if x.strip()})
    except ValueError:
        raise HTTPException(status_code=422, detail="patient_ids: ids separados por coma")
    if not ids or len(ids) > MAX_PACIENTES_RESENAS or min(ids) <= 0:
        raise HTTPException(status_code=422, detail=f"patient_ids: de 1 a {MAX_PACIENTES_RESENAS} ids")
    resultado = {i: ResenaDelPaciente(veces=0) for i in ids}
    for m in contactos_hoy.leer_marcas(db, clinic_id, ids):
        if m.motivo != "RESENA" or m.accion != "ESCRITO":
            continue
        r = resultado[m.patient_user_id]
        r.veces += 1
        if r.ultima is None or m.fecha > r.ultima:
            r.ultima = m.fecha
    cfg = contactos_config.leer(db, clinic_id)
    try:
        armados = contactos_hoy.mensajes_de_resena(db, clinic_id, ids, cfg)
    except contactos_hoy._LecturaFallida:
        armados = {}
    for paciente_id, armado in armados.items():
        r = resultado[paciente_id]
        r.numero_whatsapp = armado.numero
        r.contacto = armado.contacto
        r.mensaje = armado.mensaje
    return Resenas(link_configurado=bool(cfg.link_resena), pacientes=resultado)


# ---------------------------------------------------------------------------
# Configuración (#558)
# ---------------------------------------------------------------------------

def _puede_editar(clinic_id: int, user_id: int, roles: list) -> bool:
    """El admin y el dueño de la clínica, como la configuración de Cobros (#371). Si no se puede
    preguntar quién es el dueño, nadie más que el admin."""
    if "ADMIN" in roles:
        return True
    try:
        return contactos_hoy.duenio_de_la_clinica(clinic_id) == user_id
    except contactos_hoy._LecturaFallida:
        return False


def _config_respuesta(cfg: "contactos_config.Config", puede_editar: bool) -> ConfigDeContactos:
    return ConfigDeContactos(
        link_resena=cfg.link_resena,
        textos=TextosDeContactos(**cfg.textos),
        textos_de_fabrica=TextosDeContactos(**contactos_config.DE_FABRICA),
        marcadores=list(contactos_config.MARCADORES),
        dias_control=cfg.dias_control,
        tope_diario=cfg.tope_diario,
        motivos_activos=list(cfg.motivos_activos),
        puede_editar=puede_editar,
    )


def _texto_valido(nombre: str, texto: Optional[str]) -> Optional[str]:
    """`None` si está vacío (vuelve a lo de fábrica); 422 si trae un marcador que no existe."""
    texto = (texto or "").strip()
    if not texto:
        return None
    if len(texto) > contactos_config.LARGO_MAXIMO_TEXTO:
        raise HTTPException(status_code=422, detail=f"El texto de {nombre} pasa de {contactos_config.LARGO_MAXIMO_TEXTO} caracteres")
    desconocidos = contactos_config.marcadores_desconocidos(texto)
    if desconocidos:
        raise HTTPException(
            status_code=422,
            detail=f"El texto de {nombre} tiene un marcador que no existe: "
                   + ", ".join("{" + d + "}" for d in desconocidos)
                   + ". Los que sí: " + ", ".join("{" + m + "}" for m in contactos_config.MARCADORES),
        )
    return texto


def _link_valido(link: Optional[str]) -> Optional[str]:
    link = (link or "").strip()
    if not link:
        return None
    if not contactos_config.link_valido(link):
        raise HTTPException(status_code=422, detail="El link de reseña tiene que empezar con https://")
    return link


@router.get("/config", response_model=ConfigDeContactos)
def ver_config(
    clinic_id: int = Depends(get_clinic_id),
    user_id: int = Depends(get_user_id),
    roles: list = Depends(get_roles),
    db: Session = Depends(get_db),
):
    """La configuración de la clínica. La leen todos los que entran; `puede_editar` dice quién la cambia."""
    return _config_respuesta(contactos_config.leer(db, clinic_id), _puede_editar(clinic_id, user_id, roles))


@router.put("/config", response_model=ConfigDeContactos)
def guardar_config(
    pedido: ConfigPedido,
    clinic_id: int = Depends(get_clinic_id),
    user_id: int = Depends(get_user_id),
    roles: list = Depends(get_roles),
    db: Session = Depends(get_db),
):
    """Guarda la configuración entera. Sólo el admin y el dueño de la clínica (403 al resto)."""
    if not _puede_editar(clinic_id, user_id, roles):
        raise HTTPException(status_code=403, detail="La configuración de los mensajes la cambia el dueño de la clínica")
    textos = {}
    for nombre in contactos_config.TEXTOS:
        valor = _texto_valido(nombre, getattr(pedido.textos, nombre))
        if valor and not contactos_config.es_el_de_fabrica(nombre, valor):
            textos[nombre] = valor
    cfg = contactos_config.Config(
        link_resena=_link_valido(pedido.link_resena),
        textos=textos,
        dias_control=pedido.dias_control,
        tope_diario=pedido.tope_diario,
        motivos_activos=tuple(m for m in contactos_config.MOTIVOS_DEL_PANEL if m in pedido.motivos_activos),
    )
    contactos_config.guardar(db, clinic_id, cfg)
    logger.info("contactos_hoy config clinic_id=%s user_id=%s textos_propios=%s link=%s",
                clinic_id, user_id, ",".join(sorted(textos)), bool(cfg.link_resena))
    return _config_respuesta(contactos_config.leer(db, clinic_id), True)


# El paciente de ejemplo de la vista previa: nombres que no son de nadie.
_EJEMPLO = {
    "control": dict(motivo="CONTROL", contacto_nombre="Juan Pérez", es_tutor=False, pacientes=["Juan"]),
    "chicos": dict(motivo="CONTROL", contacto_nombre="Laura Gómez", es_tutor=True, pacientes=["Tomás", "Sofía"]),
    "presupuesto": dict(motivo="PRESUPUESTO", contacto_nombre="Carlos Díaz", es_tutor=False, pacientes=["Carlos"]),
    "resena": dict(motivo="RESENA", contacto_nombre="María López", es_tutor=False, pacientes=["María"]),
}


@router.post("/config/vista-previa", response_model=VistaPrevia)
def vista_previa(
    pedido: VistaPreviaPedido,
    clinic_id: int = Depends(get_clinic_id),
    user_id: int = Depends(get_user_id),
    db: Session = Depends(get_db),
):
    """
    Cómo queda un texto (vacío = el de fábrica) con un paciente de ejemplo, armado igual que en el
    panel. No guarda nada. El profesional es quien pregunta, si es odontólogo de la clínica.
    """
    texto = _texto_valido(pedido.cual, pedido.texto)
    guardada = contactos_config.leer(db, clinic_id)
    cfg = contactos_config.Config(
        link_resena=_link_valido(pedido.link_resena) or guardada.link_resena or "https://g.page/r/tu-clinica/review",
        textos={pedido.cual: texto} if texto and not contactos_config.es_el_de_fabrica(pedido.cual, texto) else {},
    )
    ejemplo = _EJEMPLO[pedido.cual]
    profesional = contactos_hoy.contacto.nombres_de({user_id}, clinic_id).get(user_id)
    return VistaPrevia(mensaje=contactos_hoy.armar_mensaje(
        cfg, ejemplo["motivo"], ejemplo["contacto_nombre"], ejemplo["es_tutor"], ejemplo["pacientes"],
        profesional, contactos_hoy.leer_nombre_clinica(clinic_id), meses=7,
        fecha_presupuesto=contactos_hoy.hoy_en_cordoba() - timedelta(days=12),
    ))
