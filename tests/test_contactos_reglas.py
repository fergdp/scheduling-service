"""
Las reglas de «A quién escribirle hoy» (#556), sin base: quién entra, en qué orden, qué lo saca y
cómo se arma el mensaje. Las consultas y la API están en `test_contactos_api.py`.

Las fechas son de 2027 a propósito, como el `RelojDePrueba` de clinical-history: un `date.today()`
suelto en el código no acierta de casualidad.
"""
from datetime import date, timedelta

import pytest

import contactos_hoy as ch
from contactos_hoy import Candidato, Marca, Paciente, Presupuesto

HOY = date(2027, 3, 15)


def hace(dias):
    return HOY - timedelta(days=dias)


def presupuesto(quote_id, paciente, dias, vence=None, dentista=4):
    return Presupuesto(quote_id, paciente, dentista, hace(dias), vence)


def los(lista):
    return [(c.motivo, c.patient_user_id) for c in lista]


def candidatos(atenciones=None, presupuestos=(), orto=(), futuros=(), marcas=(), **kw):
    return ch.candidatos(HOY, atenciones or {}, list(presupuestos), set(orto), set(futuros), list(marcas), **kw)


# ---------------------------------------------------------------------------
# Control
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("dias, entra", [
    (179, False), (180, True), (547, True), (1095, True), (1096, False),
])
def test_control_entra_desde_los_180_dias_y_hasta_los_tres_anos(dias, entra):
    assert los(candidatos({10: (hace(dias), 4)})) == ([("CONTROL", 10)] if entra else [])


def test_control_primero_el_mas_cercano_al_umbral():
    atenciones = {1: (hace(700), 4), 2: (hace(200), 4), 3: (hace(181), 4), 4: (hace(548), 4), 5: (hace(547), 4)}
    assert [c.patient_user_id for c in candidatos(atenciones)] == [3, 2, 5, 4, 1]


def test_con_la_ortodoncia_abierta_no_entra_a_control():
    """A ése lo llama «Controles a agendar» (#354)."""
    assert candidatos({10: (hace(200), 4)}, orto={10}) == []


def test_con_un_turno_por_venir_no_entra_a_nada():
    assert candidatos({10: (hace(200), 4)}, [presupuesto(1, 11, 10)], futuros={10, 11}) == []


def test_el_control_lleva_el_profesional_de_la_ultima_atencion():
    assert candidatos({10: (hace(200), 7)})[0].profesional_user_id == 7


def test_otro_intervalo_de_control():
    assert los(candidatos({10: (hace(100), 4)}, dias_control=90)) == [("CONTROL", 10)]


def test_ultima_atencion_es_la_mas_nueva_entre_prestacion_y_turno_atendido():
    prestaciones = {10: (hace(300), 4), 11: (hace(100), 4), 12: (hace(50), 4)}
    atendidos = {10: (hace(200), 9), 11: (hace(150), 9), 13: (hace(400), 9), 12: (hace(50), 9)}
    assert ch.ultimas_atenciones(prestaciones, atendidos) == {
        10: (hace(200), 9),   # el turno es más nuevo
        11: (hace(100), 4),   # la prestación es más nueva
        12: (hace(50), 4),    # mismo día: gana la prestación, que dice quién atendió
        13: (hace(400), 9),   # sólo turno
    }


# ---------------------------------------------------------------------------
# Presupuesto
# ---------------------------------------------------------------------------

def test_presupuesto_va_antes_que_control_y_el_mas_viejo_primero():
    lista = candidatos({1: (hace(200), 4)}, [presupuesto(20, 2, 10), presupuesto(21, 3, 30)])
    assert los(lista) == [("PRESUPUESTO", 3), ("PRESUPUESTO", 2), ("CONTROL", 1)]


def test_un_paciente_sale_una_sola_vez_con_su_motivo_de_mas_prioridad():
    lista = candidatos({2: (hace(200), 4)}, [presupuesto(20, 2, 10), presupuesto(21, 2, 30)])
    assert los(lista) == [("PRESUPUESTO", 2)]
    assert lista[0].presupuesto.quote_id == 21


def test_el_presupuesto_trae_su_dentista():
    assert candidatos(presupuestos=[presupuesto(20, 2, 10, dentista=8)])[0].profesional_user_id == 8


# ---------------------------------------------------------------------------
# Marcas
# ---------------------------------------------------------------------------

def marca(paciente, motivo, accion, dias_atras=0, quote_id=None, hasta=None):
    return Marca(paciente, motivo, accion, hace(dias_atras), quote_id, hasta)


@pytest.mark.parametrize("dias_atras, sale", [(0, False), (59, False), (60, True)])
def test_escrito_por_control_no_vuelve_por_60_dias(dias_atras, sale):
    lista = candidatos({10: (hace(400), 4)}, marcas=[marca(10, "CONTROL", "ESCRITO", dias_atras)])
    assert los(lista) == ([("CONTROL", 10)] if sale else [])


@pytest.mark.parametrize("dias_atras, sale", [(6, False), (7, True)])
def test_cualquier_mensaje_da_7_dias_de_respiro_tambien_la_resena(dias_atras, sale):
    lista = candidatos(presupuestos=[presupuesto(20, 10, 30)], marcas=[marca(10, "RESENA", "ESCRITO", dias_atras)])
    assert los(lista) == ([("PRESUPUESTO", 10)] if sale else [])


def test_un_presupuesto_escrito_no_vuelve_pero_otro_del_mismo_paciente_si():
    marcas = [marca(10, "PRESUPUESTO", "ESCRITO", 30, quote_id=20)]
    lista = candidatos(presupuestos=[presupuesto(20, 10, 40), presupuesto(21, 10, 10)], marcas=marcas)
    assert [c.presupuesto.quote_id for c in lista] == [21]


def test_un_presupuesto_descartado_no_vuelve():
    marcas = [marca(10, "PRESUPUESTO", "NO_SUGERIR", 0, quote_id=20)]
    assert candidatos(presupuestos=[presupuesto(20, 10, 40)], marcas=marcas) == []


def test_un_presupuesto_descartado_no_saca_al_paciente_de_control():
    marcas = [marca(10, "PRESUPUESTO", "NO_SUGERIR", 1, quote_id=20)]
    assert los(candidatos({10: (hace(200), 4)}, [presupuesto(20, 10, 40)], marcas=marcas)) == [("CONTROL", 10)]


def test_no_sugerir_control_es_para_siempre():
    lista = candidatos({10: (hace(900), 4)}, marcas=[marca(10, "CONTROL", "NO_SUGERIR", 800)])
    assert lista == []


@pytest.mark.parametrize("hasta, sale", [(HOY, False), (HOY - timedelta(days=1), True)])
def test_pospuesto_vuelve_al_dia_siguiente_de_hasta(hasta, sale):
    lista = candidatos({10: (hace(200), 4)}, marcas=[marca(10, "CONTROL", "POSPUESTO", 7, hasta=hasta)])
    assert los(lista) == ([("CONTROL", 10)] if sale else [])


def test_pospuesto_saca_solo_ese_motivo():
    marcas = [marca(10, "PRESUPUESTO", "POSPUESTO", 1, quote_id=20, hasta=HOY + timedelta(days=7))]
    assert los(candidatos({10: (hace(200), 4)}, [presupuesto(20, 10, 30)], marcas=marcas)) == [("CONTROL", 10)]


def test_no_sugerir_nada_lo_saca_de_todo():
    marcas = [marca(10, "CONTROL", "NO_SUGERIR_NADA", 500)]
    assert candidatos({10: (hace(200), 4)}, [presupuesto(20, 10, 30)], marcas=marcas) == []


def test_sin_lecturas_ese_motivo_no_sale():
    lista = candidatos({10: (hace(200), 4)}, [presupuesto(20, 11, 30)], con_presupuestos=False)
    assert los(lista) == [("CONTROL", 10)]
    lista = candidatos({10: (hace(200), 4)}, [presupuesto(20, 11, 30)], con_controles=False)
    assert los(lista) == [("PRESUPUESTO", 11)]


# ---------------------------------------------------------------------------
# Del candidato al aviso: baja, teléfono, familias, tope
# ---------------------------------------------------------------------------

TEL_MAMA = "351 555-1111"


def control(paciente, dias=200, profesional=4):
    return Candidato("CONTROL", paciente, dias, profesional)


def tutora(*hijos, telefono=TEL_MAMA, nombre="Laura Gómez"):
    return {h: {"menor": True, "tutores": [{"nombre": nombre, "telefono": telefono}]} for h in hijos}


def armar(lista, pacientes, datos=None, tope=ch.TOPE_DIARIO):
    return ch.armar_avisos(HOY, lista, pacientes, datos or {}, tope=tope)


def test_un_dado_de_baja_o_de_otra_clinica_no_sale():
    pacientes = {10: Paciente("Juan", "351 555-0010", True)}
    avisos, sin_tel = armar([control(10), control(11)], pacientes)
    assert avisos == [] and sin_tel == []


def test_sin_telefono_valido_va_aparte_y_no_ocupa_lugar_del_tope():
    pacientes = {10: Paciente("Juan", "15 5555-1234", False), 11: Paciente("Ana", "351 555-0011", False)}
    avisos, sin_tel = armar([control(10), control(11)], pacientes, tope=1)
    assert [a.pacientes[0][0].patient_user_id for a in avisos] == [11]
    assert [(c.patient_user_id, nombre) for c, nombre in sin_tel] == [(10, "Juan")]


def test_los_hermanos_van_en_un_solo_aviso_al_tutor():
    pacientes = {10: Paciente("Tomás Gómez", None, False), 11: Paciente("Sofía Gómez", None, False)}
    avisos, _ = armar([control(10), control(11, dias=300)], pacientes, tutora(10, 11))
    assert len(avisos) == 1
    assert [c.patient_user_id for c, _ in avisos[0].pacientes] == [10, 11]
    assert avisos[0].contacto == {"es_tutor": True, "nombre": "Laura Gómez", "telefono": TEL_MAMA}
    assert avisos[0].numero == "5493515551111"


def test_dos_adultos_con_el_mismo_numero_no_se_juntan_el_segundo_espera():
    pacientes = {10: Paciente("Juan", TEL_MAMA, False), 11: Paciente("Ana", TEL_MAMA, False)}
    avisos, _ = armar([control(10), control(11)], pacientes)
    assert [[c.patient_user_id for c, _ in a.pacientes] for a in avisos] == [[10]]


def test_un_numero_no_recibe_dos_motivos_el_mismo_dia():
    pacientes = {10: Paciente("Tomás", None, False), 11: Paciente("Sofía", None, False)}
    lista = [Candidato("PRESUPUESTO", 10, 10, 4, presupuesto(20, 10, 10)), control(11)]
    avisos, _ = armar(lista, pacientes, tutora(10, 11))
    assert [(a.motivo, len(a.pacientes)) for a in avisos] == [("PRESUPUESTO", 1)]


def test_el_tope_cuenta_avisos_y_un_hermano_se_suma_aunque_este_lleno():
    pacientes = {i: Paciente(f"P{i}", f"351 555-{1000 + i}", False) for i in range(1, 15)}
    pacientes[20] = Paciente("Sofía", None, False)
    datos = tutora(20, telefono="351 555-1001")
    datos[1] = {"menor": True, "tutores": [{"nombre": "Laura", "telefono": "351 555-1001"}]}
    lista = [control(i) for i in range(1, 15)] + [control(20)]
    avisos, _ = armar(lista, pacientes, datos, tope=3)
    assert len(avisos) == 3
    assert [c.patient_user_id for c, _ in avisos[0].pacientes] == [1, 20]


def test_la_razon():
    p = presupuesto(20, 10, 12)
    aviso = ch.Aviso("PRESUPUESTO", "549", {}, 4, [(Candidato("PRESUPUESTO", 10, 12, 4, p), "Juan")])
    assert ch.razon(aviso) == f"Presupuesto del {hace(12):%d/%m} sin respuesta"
    uno = ch.Aviso("CONTROL", "549", {}, 4, [(control(10, dias=245), "Juan")])
    assert ch.razon(uno) == "No viene hace 8 meses"
    dos = ch.Aviso("CONTROL", "549", {}, 4, [(control(10, dias=400), "A"), (control(11, dias=200), "B")])
    assert ch.razon(dos) == "No vienen hace 6 meses"
    pocos = ch.Aviso("CONTROL", "549", {}, 4, [(control(10, dias=20), "A")])
    assert ch.razon(pocos) == "No viene hace 20 días"


# ---------------------------------------------------------------------------
# El mensaje
# ---------------------------------------------------------------------------

def test_control_a_si_mismo():
    texto = ch.mensaje("CONTROL", "JUAN CARLOS PÉREZ", False, [], "Analía Ríos", "Consultorio Sur.", meses=7)
    assert texto == (
        "Hola Juan, ¿cómo estás? Soy Analía Ríos, de Consultorio Sur. Ya pasaron 7 meses desde tu "
        "última consulta: es un buen momento para un control. ¿Querés que te reservemos un turno?"
    )


def test_control_al_tutor_nombra_a_los_chicos():
    texto = ch.mensaje("CONTROL", "laura gómez", True, ["Tomás", "Sofía"], "Analía Ríos", None, meses=6)
    assert texto == (
        "Hola Laura, ¿cómo estás? Soy Analía Ríos. Ya pasaron 6 meses desde la última consulta de "
        "Tomás y Sofía: es un buen momento para un control. ¿Querés que les reservemos un turno?"
    )


def test_control_al_tutor_de_un_solo_chico():
    texto = ch.mensaje("CONTROL", "Laura", True, ["Tomás"], None, "Consultorio Sur", meses=6)
    assert "Te escribimos de Consultorio Sur." in texto
    assert "de Tomás:" in texto and texto.endswith("¿Querés que le reservemos un turno?")


def test_sin_nombres_ni_meses_igual_se_entiende():
    texto = ch.mensaje("CONTROL", None, True, [], None, None)
    assert texto.startswith("Hola, ¿cómo estás? Pasó un tiempo desde la última consulta de quien tenés a cargo")


def test_presupuesto():
    texto = ch.mensaje("PRESUPUESTO", "Carlos", False, [], "Analía Ríos", None,
                       fecha_presupuesto=date(2027, 3, 3))
    assert texto == (
        "Hola Carlos, ¿cómo estás? Soy Analía Ríos. Quería saber si te quedó alguna duda con el "
        "presupuesto del 03/03. Si querés, lo vemos juntos."
    )


def test_presupuesto_vencido_y_al_tutor():
    texto = ch.mensaje("PRESUPUESTO", "Laura", True, ["Tomás"], None, "Consultorio Sur",
                       fecha_presupuesto=date(2027, 3, 3), presupuesto_vencido=True)
    assert "Queríamos saber si te quedó alguna duda con el presupuesto de Tomás del 03/03." in texto
    assert texto.endswith("Como ya pasó su fecha de validez, te lo podemos actualizar.")


def test_presupuestos_de_dos_chicos_sin_fecha():
    texto = ch.mensaje("PRESUPUESTO", "Laura", True, ["Tomás", "Sofía"], None, None,
                       fecha_presupuesto=date(2027, 3, 3))
    assert "con los presupuestos de Tomás y Sofía. " in texto


def test_presupuesto_al_tutor_sin_el_nombre_del_chico():
    texto = ch.mensaje("PRESUPUESTO", "Laura", True, [""], None, None)
    assert "el presupuesto de quien tenés a cargo." in texto


@pytest.mark.parametrize("motivo", ["CONTROL", "PRESUPUESTO"])
@pytest.mark.parametrize("es_tutor", [True, False])
def test_ningun_mensaje_dice_cita(motivo, es_tutor):
    texto = ch.mensaje(motivo, "Ana", es_tutor, ["Tomás"], "Analía", "Sur", meses=7,
                       fecha_presupuesto=date(2027, 3, 3), presupuesto_vencido=True)
    assert "cita" not in texto.lower()


def test_primer_nombre_respeta_lo_que_ya_mezcla():
    assert ch.primer_nombre("McKenzie Smith") == "McKenzie"
    assert ch.primer_nombre("   ") == ""


@pytest.mark.parametrize("accion", ["POSPUESTO", "NO_SUGERIR", "ESCRITO"])
def test_un_paciente_con_una_marca_de_hoy_no_vuelve_hoy_por_otro_motivo(accion):
    marcas = [marca(10, "PRESUPUESTO", accion, 0, quote_id=20, hasta=HOY + timedelta(days=7) if accion == "POSPUESTO" else None)]
    assert candidatos({10: (hace(200), 4)}, [presupuesto(20, 10, 40)], marcas=marcas) == []


def test_lo_escrito_hoy_queda_anotado_por_motivo():
    b = ch.bloqueos([marca(10, "PRESUPUESTO", "ESCRITO", 0, quote_id=20), marca(11, "RESENA", "ESCRITO", 0),
                     marca(12, "CONTROL", "POSPUESTO", 0, hasta=HOY), marca(13, "CONTROL", "ESCRITO", 1)], HOY)
    assert b.escritos_hoy == {(10, "PRESUPUESTO"), (11, "RESENA")}
    assert b.tocados_hoy == {10, 11, 12}


def test_un_numero_ocupado_no_recibe_nada():
    pacientes = {10: Paciente("Juan", "351 555-0010", False), 11: Paciente("Ana", "351 555-0011", False)}
    avisos, _ = ch.armar_avisos(HOY, [control(10), control(11)], pacientes, {},
                                numeros_ocupados=frozenset({"5493515550010"}))
    assert [a.pacientes[0][0].patient_user_id for a in avisos] == [11]
