"""
A quién se le escribe por WhatsApp por un turno o por la lista de espera (#417).

Es la regla del consultorio: «si el paciente es menor, se le escribe al tutor y se nombra al
chico» (Analía lo confirmó el 2026-10-12: siempre al adulto, aunque el adolescente tenga celular).
Ortodoncia ya la aplicaba en el backend (`OrthoRecallService.aQuienEscribirle`, en
clinical-history-service); ésta es la misma tabla, copiada:

| El paciente | A quién |
|---|---|
| No tiene a nadie a cargo | A él, tenga o no teléfono |
| Está a cargo de alguien, y es menor o no tiene fecha de nacimiento | Al tutor: a su teléfono, o al de la ficha del paciente si el tutor no tiene |
| Está a cargo de alguien, es mayor de edad y tiene teléfono | A él |
| Está a cargo de alguien, es mayor de edad y no tiene teléfono | Al tutor que tenga uno |

⚠️ La regla no puede depender de «es menor» a secas: casi ningún paciente a cargo tiene fecha de
nacimiento (medido en la base local para el #354: ninguno de 90). Un «no se sabe» cuenta como
menor. Si cambia, cambia en los dos servicios (y en Cobros, `destinatarioDelPago` de
presupuestos-front, que elige a quién va el comprobante con su propia regla).

Dos diferencias con ortodoncia, las dos por los datos que hay acá:
- El teléfono propio es el vigente o, si no hay, el guardado en el turno (#313): a un mayor de
  edad con tutor y sin teléfono en su ficha, pero con uno guardado en el turno, se le escribe a él.
- A un paciente que no está entre los usuarios de la clínica no se le buscan tutores: se le
  escribe a él.

Lee tablas de dental-clinic (`users`, `patient_guardians`) con la misma conexión que
`telefonos_vigentes_de`: ver «Tablas que otro servicio lee directo» en el CLAUDE.md del monorepo.
"""
import logging
from datetime import date, datetime
from typing import Optional

from sqlalchemy import bindparam, text
from sqlalchemy.exc import SQLAlchemyError

import dependencies
from horario_atencion import HUSO_CLINICA
from schemas import ContactoParaEscribir

logger = logging.getLogger(__name__)

MAYORIA_DE_EDAD = 18


def _texto(valor) -> Optional[str]:
    """Sin los espacios de las puntas; uno vacío es «no tiene»."""
    if valor is None:
        return None
    limpio = str(valor).strip()
    return limpio or None


def _nombre(first_name, last_name) -> Optional[str]:
    return _texto(f"{first_name or ''} {last_name or ''}")


def es_menor(nacimiento: Optional[date], hoy: date) -> Optional[bool]:
    """`None` si no se sabe cuándo nació. Deja de ser menor el día que cumple 18."""
    if nacimiento is None:
        return None
    try:
        mayoria = nacimiento.replace(year=nacimiento.year + MAYORIA_DE_EDAD)
    except ValueError:  # Nació un 29 de febrero y ese año no es bisiesto: el 28, como `plusYears` en Java
        mayoria = date(nacimiento.year + MAYORIA_DE_EDAD, 2, 28)
    return hoy < mayoria


def a_quien_escribirle(nombre_paciente, telefono_paciente, tutores: list, menor: Optional[bool]) -> dict:
    """
    El contacto para el WhatsApp: `{"es_tutor", "nombre", "telefono"}`. Nunca `None`; el teléfono
    sí puede faltar.

    `tutores`: `[{"nombre", "telefono"}]`, en el orden de la consulta (de dos, el primero que tenga
    teléfono). `menor`: `None` si no se sabe.
    """
    telefono_propio = _texto(telefono_paciente)
    if tutores:
        con_telefono = next((t for t in tutores if _texto(t.get("telefono"))), None)
        if menor is not False:
            if con_telefono is not None:
                return {"es_tutor": True, "nombre": con_telefono["nombre"], "telefono": _texto(con_telefono["telefono"])}
            return {"es_tutor": True, "nombre": tutores[0]["nombre"], "telefono": telefono_propio}
        if telefono_propio is None and con_telefono is not None:
            return {"es_tutor": True, "nombre": con_telefono["nombre"], "telefono": _texto(con_telefono["telefono"])}
    return {"es_tutor": False, "nombre": _texto(nombre_paciente), "telefono": telefono_propio}


def datos_de_contacto(patient_user_ids, clinic_id) -> dict:
    """
    De cada paciente, si es menor y sus tutores: `{id: {"menor": bool|None, "tutores": [...]}}`.
    Dos consultas para toda la página, como `telefonos_vigentes_de`.

    - Todo de la clínica de quien pide (la del turno): el paciente, el vínculo y el tutor, igual
      que en ortodoncia. Un paciente que se pasó a otra clínica no trae tutores de allá.
    - Un tutor dado de baja (`deactivated_at`, #375) no viene: si era el único, el paciente queda
      como quien no tiene a nadie a cargo. No se mira `is_enabled`: está en falso también para
      quien espera aprobación.
    - Un paciente que no está entre los usuarios de la clínica no aparece en el mapa: el llamador
      lo trata como sin tutores.
    - ⚠️ Si la consulta falla (una columna que dental-clinic renombró), anota un ERROR y devuelve
      un mapa vacío: cada paciente es su propio contacto, como antes del #417. Es un dato para un
      WhatsApp; la agenda no se cae por eso.
    """
    ids = sorted({i for i in patient_user_ids if i})
    if not ids:
        return {}

    db = dependencies.ClinicSessionLocal()
    try:
        pacientes = db.execute(
            text("SELECT user_id, date_of_birth FROM users WHERE user_id IN :ids AND clinic_id = :c")
            .bindparams(bindparam("ids", expanding=True)),
            {"ids": ids, "c": clinic_id},
        ).all()
        tutores = db.execute(
            text(
                "SELECT g.dependent_user_id, t.first_name, t.last_name, t.phone "
                "FROM patient_guardians g "
                "JOIN users t ON t.user_id = g.guardian_user_id "
                "WHERE g.dependent_user_id IN :ids "
                "AND g.clinic_id = :c AND t.clinic_id = :c "
                "AND t.deactivated_at IS NULL "
                "ORDER BY g.dependent_user_id, t.user_id"
            ).bindparams(bindparam("ids", expanding=True)),
            {"ids": ids, "c": clinic_id},
        ).all()
    except SQLAlchemyError as exc:
        # Sin el mensaje de la base: puede traer valores. Con el tipo alcanza para buscarlo.
        logger.error("No se pudieron leer los tutores para el WhatsApp (%s): se le escribe al paciente",
                     type(exc).__name__)
        return {}
    finally:
        db.close()

    hoy = datetime.now(HUSO_CLINICA).date()
    datos = {
        fila[0]: {"menor": es_menor(_fecha(fila[1]), hoy), "tutores": []}
        for fila in pacientes
    }
    for paciente, first_name, last_name, telefono in tutores:
        if paciente in datos:
            datos[paciente]["tutores"].append({"nombre": _nombre(first_name, last_name), "telefono": telefono})
    return datos


def _fecha(valor) -> Optional[date]:
    """MySQL devuelve `date`; SQLite, el texto. Una fecha ilegible es «no se sabe»."""
    if isinstance(valor, datetime):
        return valor.date()
    if valor is None or isinstance(valor, date):
        return valor
    try:
        return date.fromisoformat(str(valor)[:10])
    except ValueError:
        return None


def contacto_de(patient_user_id, nombre_paciente, telefono_paciente, datos: dict) -> ContactoParaEscribir:
    """
    El contacto de un paciente con los datos de `datos_de_contacto`, ya como el modelo de la
    respuesta. ⚠️ No asignarle un `dict` a `contacto`: Pydantic avisa al serializar con un warning
    que trae el valor, teléfonos incluidos, y el aviso va al log de errores.
    """
    suyos = datos.get(patient_user_id) or {}
    return ContactoParaEscribir(
        **a_quien_escribirle(nombre_paciente, telefono_paciente, suyos.get("tutores", []), suyos.get("menor")),
    )
