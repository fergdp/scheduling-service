"""
Métricas de «A quién escribirle hoy» (#559): las cuentas contra tablas de verdad (las mismas de
`test_contactos_api.py`) y la API. Las marcas se cargan directo, con la fecha que hace falta.
"""
from datetime import datetime, timedelta

import pytest

import contactos_hoy
from conftest import TestingSessionLocal
from models import Appointment, ContactMark
from test_contactos_api import HOY, SECRETO, URL, clinica, hace  # noqa: F401 (clinica es un fixture)

METRICAS = f"{URL}/metricas"


def marca(paciente, motivo, dias, accion="ESCRITO", quote_id=None, clinic_id=1):
    db = TestingSessionLocal()
    m = ContactMark(clinic_id=clinic_id, patient_user_id=paciente, motivo=motivo, quote_id=quote_id,
                    accion=accion, fecha=hace(dias), marked_by_user_id=4)
    db.add(m)
    db.commit()
    mark_id = m.mark_id
    db.close()
    return mark_id


def deshacer(mark_id, dias):
    db = TestingSessionLocal()
    original = db.get(ContactMark, mark_id)
    db.add(ContactMark(clinic_id=original.clinic_id, patient_user_id=original.patient_user_id, motivo=original.motivo,
                       accion="DESHECHO", fecha=hace(dias), anula_a_id=mark_id, marked_by_user_id=4))
    db.commit()
    db.close()


def turno_creado(paciente, dias, clinic_id=1, borrado=False):
    """Un turno creado hace `dias` (a las 15 UTC, mediodía en Córdoba), para dentro de un mes."""
    creado = datetime.combine(hace(dias), datetime.min.time()) + timedelta(hours=15)
    db = TestingSessionLocal()
    db.add(Appointment(clinic_id=clinic_id, patient_user_id=paciente, dentist_user_id=4, patient_name="x",
                       start_time_utc=creado + timedelta(days=30), end_time_utc=creado + timedelta(days=30, minutes=30),
                       created_at=creado, deleted_at=creado if borrado else None))
    db.commit()
    db.close()


def aceptar(clinica, quote_id, dias, estado="ACCEPTED"):
    clinica.sql("UPDATE dental_quoting_db.quote SET estado = :e, accepted_at = :a WHERE id = :id",
                e=estado, a=f"{hace(dias).isoformat()} 15:00:00", id=quote_id)


def medir(client, esperado=200, **params):
    r = client.get(METRICAS, params=params)
    assert r.status_code == esperado, r.text
    return r.json()


def test_un_control_vuelve_con_un_turno_creado_dentro_de_los_30_dias(clinica, client):
    marca(10, "CONTROL", 60); turno_creado(10, 45)        # volvió a los 15 días
    marca(11, "CONTROL", 60); turno_creado(11, 29)        # a los 31: no
    marca(12, "CONTROL", 60); turno_creado(12, 50, borrado=True)  # borrado: no
    marca(13, "CONTROL", 60); turno_creado(13, 60)        # el mismo día: sí
    marca(14, "CONTROL", 10)                              # hace 10 días: en curso
    r = medir(client)["control"]
    assert r == {"escritos": 5, "funcionaron": 2, "en_curso": 1, "cumplidos": 4, "funcionaron_cumplidos": 2,
                 "pospuestos": 0, "descartados": 0}


def test_el_porcentaje_no_cuenta_las_recientes_que_ya_volvieron(clinica, client):
    """Una reciente que volvió no está cumplida: si entrara, el porcentaje daría de más."""
    for paciente in (10, 11, 12):
        marca(paciente, "CONTROL", 5)
        turno_creado(paciente, 3)
    marca(13, "CONTROL", 5)
    marca(14, "CONTROL", 40)
    r = medir(client)["control"]
    assert (r["funcionaron"], r["en_curso"], r["cumplidos"], r["funcionaron_cumplidos"]) == (3, 1, 1, 0)


def test_los_bordes_de_los_30_dias(clinica, client):
    marca(10, "CONTROL", 40); turno_creado(10, 10)  # a los 30 justos: volvió
    marca(11, "CONTROL", 30)                         # hoy es su día 30: todavía puede
    marca(12, "CONTROL", 31)                         # ayer fue su día 30: cumplida
    r = medir(client)["control"]
    assert (r["funcionaron"], r["en_curso"], r["cumplidos"]) == (1, 1, 2)


def test_el_dia_del_turno_es_el_de_cordoba(clinica, client):
    """Creado a la 01:00 UTC del día 31 es el día 30 en Córdoba: entra."""
    marca(10, "CONTROL", 40)
    db = TestingSessionLocal()
    creado = datetime.combine(hace(9), datetime.min.time()) + timedelta(hours=1)
    db.add(Appointment(clinic_id=1, patient_user_id=10, dentist_user_id=4, patient_name="x",
                       start_time_utc=creado + timedelta(days=5), end_time_utc=creado + timedelta(days=5, minutes=30),
                       created_at=creado))
    db.commit()
    db.close()
    assert medir(client)["control"]["funcionaron"] == 1


def test_un_control_vuelve_con_una_prestacion(clinica, client):
    marca(10, "CONTROL", 40)
    clinica.prestacion(10, 20)
    marca(11, "CONTROL", 40)
    clinica.prestacion(11, 50)  # de antes de la marca: no cuenta
    r = medir(client)["control"]
    assert (r["funcionaron"], r["en_curso"]) == (1, 0)


def test_una_marca_deshecha_no_cuenta_aunque_se_deshaga_despues_del_periodo(clinica, client):
    deshacer(marca(10, "CONTROL", 50), 5)
    marca(11, "CONTROL", 50)
    r = medir(client, desde=hace(60).isoformat(), hasta=hace(30).isoformat())
    assert r["control"]["escritos"] == 1


def test_un_presupuesto_aceptado_en_30_dias(clinica, client):
    clinica.presupuesto(30, 10, 70); marca(10, "PRESUPUESTO", 60, quote_id=30); aceptar(clinica, 30, 50)
    clinica.presupuesto(31, 11, 70); marca(11, "PRESUPUESTO", 60, quote_id=31); aceptar(clinica, 31, 20)  # a los 40
    clinica.presupuesto(32, 12, 70); marca(12, "PRESUPUESTO", 60, quote_id=32); aceptar(clinica, 32, 55, "CONVERTED")
    clinica.presupuesto(33, 13, 70); marca(13, "PRESUPUESTO", 60, quote_id=33)
    clinica.sql("UPDATE dental_quoting_db.quote SET estado = 'REJECTED', accepted_at = :a WHERE id = 33",
                a=f"{hace(55).isoformat()} 15:00:00")  # rechazado: no
    r = medir(client)["presupuesto"]
    assert (r["escritos"], r["funcionaron"], r["en_curso"], r["funcionaron_cumplidos"]) == (4, 2, 0, 2)


def test_cuenta_la_version_nueva_del_presupuesto_escrito(clinica, client):
    clinica.presupuesto(30, 10, 70, estado="SUPERSEDED")
    marca(10, "PRESUPUESTO", 60, quote_id=30)
    clinica.presupuesto(31, 10, 55, version_de=30)
    aceptar(clinica, 31, 50)
    assert medir(client)["presupuesto"]["funcionaron"] == 1


def test_resenas_pospuestos_descartados_y_semanas(clinica, client):
    marca(10, "RESENA", 3)
    marca(11, "RESENA", 3)
    marca(12, "RESENA", 10)
    marca(13, "CONTROL", 3, accion="POSPUESTO")
    marca(14, "CONTROL", 3, accion="NO_SUGERIR")
    marca(15, "PRESUPUESTO", 3, accion="NO_SUGERIR_NADA")
    r = medir(client)
    assert r["resena"] == {"escritos": 3, "funcionaron": None, "en_curso": None, "cumplidos": None,
                           "funcionaron_cumplidos": None, "pospuestos": 0, "descartados": 0}
    assert (r["control"]["pospuestos"], r["control"]["descartados"], r["presupuesto"]["descartados"]) == (1, 1, 1)
    # HOY es lunes 15/3/2027: hace 3 días es el viernes de la semana del 8, hace 10, la del 1.
    assert r["semanas"] == [
        {"lunes": "2027-03-01", "control": 0, "presupuesto": 0, "resena": 1},
        {"lunes": "2027-03-08", "control": 0, "presupuesto": 0, "resena": 2},
    ]


def test_el_periodo_por_defecto_son_90_dias_y_las_marcas_de_afuera_no_cuentan(clinica, client):
    marca(10, "RESENA", 89)
    marca(11, "RESENA", 90)
    r = medir(client)
    assert (r["desde"], r["hasta"], r["ventana_dias"]) == (hace(89).isoformat(), HOY.isoformat(), 30)
    assert r["resena"]["escritos"] == 1


def test_lo_de_otra_clinica_no_cuenta(clinica, client):
    marca(10, "CONTROL", 40, clinic_id=2)
    turno_creado(10, 30, clinic_id=2)
    marca(11, "CONTROL", 40)
    turno_creado(11, 30, clinic_id=2)  # el turno de otra clínica no la hace volver
    r = medir(client)["control"]
    assert (r["escritos"], r["funcionaron"]) == (1, 0)


def test_la_prestacion_y_el_presupuesto_de_otra_clinica_no_cuentan(clinica, client):
    """El mismo paciente con una historia en otra clínica, y un presupuesto suyo aceptado allá."""
    marca(10, "CONTROL", 40)
    clinica.prestacion(10, 20, clinic_id=2)
    clinica.presupuesto(30, 11, 70)
    marca(11, "PRESUPUESTO", 40, quote_id=30)
    clinica.presupuesto(31, 11, 60, version_de=30, clinic_id=2)
    aceptar(clinica, 31, 35)
    r = medir(client)
    assert (r["control"]["funcionaron"], r["presupuesto"]["funcionaron"]) == (0, 0)


def test_si_una_lectura_falla_esa_cuenta_sale_vacia(clinica, client, monkeypatch):
    marca(10, "CONTROL", 40)
    clinica.presupuesto(30, 11, 70)
    marca(11, "PRESUPUESTO", 40, quote_id=30)
    marca(12, "RESENA", 5)

    def falla(sql, params, que):
        raise contactos_hoy._LecturaFallida(que)
    monkeypatch.setattr(contactos_hoy, "_consulta", falla)
    r = medir(client)
    assert r["motivos_con_error"] == ["CONTROL", "PRESUPUESTO"]
    assert (r["control"]["escritos"], r["control"]["funcionaron"], r["control"]["en_curso"]) == (1, None, None)
    assert r["presupuesto"]["funcionaron"] is None
    assert r["resena"]["escritos"] == 1


@pytest.mark.parametrize("params", [
    {"desde": "2027-03-10", "hasta": "2027-03-01"},
    {"hasta": "2027-03-16"},
    {"desde": "2026-03-01", "hasta": "2027-03-15"},
    {"desde": "no-es-fecha"},
])
def test_periodos_invalidos(clinica, client, params):
    medir(client, esperado=422, **params)


def test_las_consultas_no_piden_columnas_clinicas_ni_montos(clinica, client, monkeypatch):
    pedidas = []
    original = contactos_hoy._consulta
    monkeypatch.setattr(contactos_hoy, "_consulta", lambda sql, p, q: pedidas.append(sql) or original(sql, p, q))
    clinica.prestacion(10, 20)
    marca(10, "CONTROL", 40)
    clinica.presupuesto(30, 11, 70)
    marca(11, "PRESUPUESTO", 40, quote_id=30)
    texto = client.get(METRICAS).text
    todo = " ".join(pedidas).lower()
    assert len(pedidas) == 2
    for columna in ["treatment_description", "debit", "notas", "total", "affiliate_number", "*"]:
        assert columna not in todo
    for prohibido in [SECRETO, "12345", "98765", '"patient_user_id"']:
        assert prohibido not in texto


def test_la_recepcion_las_ve(clinica, receptionist_client):
    assert receptionist_client.get(METRICAS).status_code == 200


def test_un_paciente_no(clinica, patient_client):
    assert patient_client.get(METRICAS).status_code == 403
