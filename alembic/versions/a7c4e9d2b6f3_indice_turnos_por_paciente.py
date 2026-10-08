"""Índice (clinic_id, patient_user_id, start_time_utc) en appointments

Revision ID: a7c4e9d2b6f3
Revises: f2a6c8d4e1b9
Create Date: 2026-10-08

Para «¿qué turnos tienen por delante estos pacientes?» (#355: `POST /v1/appointments/upcoming-by-patient`,
y la misma cuenta que ya hacía la lista de espera). La consulta filtra por clínica, por una lista de
pacientes y por «de hoy en adelante»: con este índice MySQL lee, de cada paciente, sólo sus turnos
que vienen. Sin él entra por el índice de `patient_user_id` solo y recorre el historial entero de
cada uno, o por el de la clínica y recorre los turnos que vienen de todos sus pacientes.

Medido en MySQL 8 sobre una copia de la base local (2026-10-08): pidiendo 200 pacientes de una
clínica con 2.000, lee 405 filas con el índice y 5.356 sin él. Cuando el pedido abarca a todos los
pacientes de la clínica MySQL no lo usa, y está bien: ahí no hay nada que ahorrar.

Sólo agrega un índice: no cambia ni un dato. En InnoDB se crea sin bloquear las escrituras, pero
necesita la tabla para sí un instante: ver `_no_esperar_para_siempre`.

El modelo lo declara con el mismo nombre y las mismas columnas (`models.Appointment`); que digan lo
mismo lo cuida `tests/test_proximos_turnos.py`.
"""
from typing import Sequence, Union

from alembic import op


revision: str = 'a7c4e9d2b6f3'
down_revision: Union[str, None] = 'f2a6c8d4e1b9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

INDICE = 'ix_appointments_clinic_patient_start'

# Cuánto espera la migración, como mucho, a que le dejen la tabla.
ESPERA_MAXIMA_SEGUNDOS = 10


def _no_esperar_para_siempre() -> None:
    """
    Crear o sacar un índice necesita, por un instante, la tabla para sí. El deploy corre esto con el
    servicio atendiendo: si en ese momento hay una transacción abierta que leyó la tabla (un turno
    que se está guardando mientras se avisa a Google Calendar, por ejemplo), MySQL espera a que
    termine, y **mientras espera hace esperar también a todas las consultas nuevas a los turnos**:
    la agenda queda quieta. De fábrica esa espera no tiene tope (`lock_wait_timeout`: un año).

    Con este tope, a los 10 segundos la migración falla sin haber cambiado nada: el deploy corta ahí,
    con el servicio de antes todavía andando, y se vuelve a intentar. Va ANTES de tocar la tabla.
    """
    op.execute(f'SET SESSION lock_wait_timeout = {ESPERA_MAXIMA_SEGUNDOS}')


def upgrade() -> None:
    _no_esperar_para_siempre()
    op.create_index(INDICE, 'appointments', ['clinic_id', 'patient_user_id', 'start_time_utc'], unique=False)


def downgrade() -> None:
    _no_esperar_para_siempre()
    op.drop_index(INDICE, table_name='appointments')
