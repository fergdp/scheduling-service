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

import contactos_hoy
from dependencies import get_clinic_id, get_db, get_user_id, require_any_role
from models import ContactMark
from schemas_contactos import (
    Aviso, AvisosDelDia, MarcaGuardada, MarcaPedido, MarcasGuardadas, PacienteDelAviso,
    PedidosDeResena, SinTelefono,
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
    dia = contactos_hoy.armar_el_dia(db, clinic_id, dentist_id=dentist_id)
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
            mensaje=contactos_hoy.mensaje(
                a.motivo, a.contacto["nombre"], es_tutor, nombres, profesional, dia.clinica,
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
        tope=contactos_hoy.TOPE_DIARIO,
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


@router.get("/resenas", response_model=dict[int, PedidosDeResena])
def pedidos_de_resena(
    patient_ids: str = Query(..., description="Ids separados por coma"),
    clinic_id: int = Depends(get_clinic_id),
    db: Session = Depends(get_db),
):
    """
    Cuántas veces se le pidió la reseña a cada paciente y cuándo fue la última, para el botón de la
    ficha y de la agenda: una consulta para toda la página, no una por fila. Sólo cuenta las marcas
    de la clínica; un id sin pedidos (o de otra clínica) sale con 0.
    """
    try:
        ids = sorted({int(x) for x in patient_ids.split(",") if x.strip()})
    except ValueError:
        raise HTTPException(status_code=422, detail="patient_ids: ids separados por coma")
    if not ids or len(ids) > MAX_PACIENTES_RESENAS or min(ids) <= 0:
        raise HTTPException(status_code=422, detail=f"patient_ids: de 1 a {MAX_PACIENTES_RESENAS} ids")
    resultado = {i: PedidosDeResena(veces=0) for i in ids}
    for m in contactos_hoy.leer_marcas(db, clinic_id, ids):
        if m.motivo != "RESENA" or m.accion != "ESCRITO":
            continue
        r = resultado[m.patient_user_id]
        r.veces += 1
        if r.ultima is None or m.fecha > r.ultima:
            r.ultima = m.fecha
    return resultado
