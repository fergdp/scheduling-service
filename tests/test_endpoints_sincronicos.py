"""
Los endpoints de turnos y de Google Calendar son `def`, no `async def` (issue #306).

SQLAlchemy acá es sincrónico y las llamadas a Google Calendar son bloqueantes (sin cliente
async): ninguno de los dos hace un `await` real. Un `async def` que hace ese trabajo adentro
bloquea el event loop del worker ENTERO mientras corre —ningún otro pedido se atiende, de
ninguna clínica— hasta que termina. Con `def`, FastAPI corre el endpoint en su pool de hilos y
el event loop queda libre.

Dos cosas se prueban acá: (1) un guard estructural, para que ningún endpoint de
`routers/appointments.py` ni `routers/oauth.py` vuelva a declararse `async def` por accidente, y
(2) la medición que pide el propio ticket: un pedido lento corriendo a la vez que uno rápido, y
que el rápido no tenga que esperarlo.
"""
import inspect
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import uvicorn

import main
import routers.appointments as ra
import routers.oauth as roauth


def test_ningun_endpoint_de_turnos_es_async():
    """Guard estructural: revierte sólo si alguien vuelve a poner `async def` sin querer."""
    asincronos = [
        route.path for route in ra.router.routes
        if inspect.iscoroutinefunction(getattr(route, "endpoint", None))
    ]
    assert asincronos == [], f"endpoints async en appointments.py: {asincronos}"


def test_ningun_endpoint_de_oauth_es_async():
    asincronos = [
        route.path for route in roauth.router.routes
        if inspect.iscoroutinefunction(getattr(route, "endpoint", None))
    ]
    assert asincronos == [], f"endpoints async en oauth.py: {asincronos}"


def test_un_pedido_lento_no_bloquea_uno_rapido(client, monkeypatch):
    """
    Medición de antes/después que pide el ticket #306: con `def`, un pedido lento (`/upcoming`,
    con una consulta simulada de 400 ms) no le hace esperar a uno rápido (`/`) que llega
    mientras el lento sigue en curso.

    `client` no se usa para pedir nada —los pedidos van por `httpx.Client()` crudo contra el
    servidor uvicorn real de abajo—: está sólo por su efecto de lado, dejar `app.dependency_overrides`
    pisado en el mismo objeto `app` que sirve ese servidor, así las rutas no piden auth real.

    ⚠️ **Tiene que ser un servidor uvicorn real, no `TestClient`/`ASGITransport` en el mismo
    proceso.** Medido: contra la app en proceso, con dos corutinas sobre el mismo event loop,
    la rápida se cuela durante los `await` genuinos de los middlewares (CORS, CSRF, rate
    limiter) ANTES de que la lenta llegue a su `time.sleep` bloqueante, y el test da un falso
    verde incluso con el bug puesto (`async def`). Contra un servidor real con dos conexiones
    de socket de verdad —lo que pasa en producción— el bloqueo se ve clarísimo: con `async def`
    saboteado a propósito, la rápida tardó 0.384s (casi lo mismo que la lenta, 0.419s); con
    `def`, 0.004s. Ver el diagnóstico en la sesión que escribió este test.
    """
    original = ra._utcnow_naive

    def _lenta():
        time.sleep(0.4)
        return original()

    monkeypatch.setattr(ra, "_utcnow_naive", _lenta)

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        puerto = s.getsockname()[1]

    config = uvicorn.Config(main.app, host="127.0.0.1", port=puerto, log_level="critical")
    server = uvicorn.Server(config)
    hilo_servidor = threading.Thread(target=server.run, daemon=True)
    hilo_servidor.start()
    base_url = f"http://127.0.0.1:{puerto}"

    try:
        with httpx.Client() as sonda:
            for _ in range(100):
                try:
                    sonda.get(f"{base_url}/", timeout=0.2)
                    break
                except httpx.TransportError:
                    time.sleep(0.05)
            else:
                raise RuntimeError("el servidor de prueba no levantó a tiempo")

        def _pedir(url):
            with httpx.Client() as c:
                t0 = time.perf_counter()
                r = c.get(f"{base_url}{url}", timeout=5)
                return r.status_code, time.perf_counter() - t0

        with ThreadPoolExecutor(max_workers=2) as pool:
            futuro_lenta = pool.submit(_pedir, "/clinic-scheduling-api/v1/appointments/upcoming")
            time.sleep(0.03)  # ventaja para que la lenta arranque primero
            futuro_rapida = pool.submit(_pedir, "/")

            status_lenta, t_lenta = futuro_lenta.result(timeout=5)
            status_rapida, t_rapida = futuro_rapida.result(timeout=5)
    finally:
        server.should_exit = True
        hilo_servidor.join(timeout=2)

    assert status_lenta == 200
    assert status_rapida == 200
    assert t_lenta > 0.35, f"la lenta terminó en {t_lenta:.3f}s — el mock del sleep no se aplicó"
    assert t_rapida < 0.15, (
        f"la rápida tardó {t_rapida:.3f}s con la lenta todavía en curso ({t_lenta:.3f}s): "
        f"el event loop parece bloqueado en vez de libre"
    )
