"""Smoke test de /metrics (observabilidad, issue #75).

Verifica el wiring de prometheus-fastapi-instrumentator: que /metrics responda
200 y publique las métricas RED de request HTTP que el dashboard de Grafana
consume. El modo multiproceso (varios workers gunicorn) solo aplica en prod con
PROMETHEUS_MULTIPROC_DIR seteada; acá corre single-process.
"""


def test_metrics_endpoint_exposes_red_metrics(client):
    # Un request cualquiera (un 404 alcanza) genera una muestra http_request_*;
    # no toca rate limit ni auth. /metrics y /health quedan excluidos del conteo.
    client.get("/__warmup_metrics__")
    response = client.get("/metrics")
    assert response.status_code == 200
    assert "http_request" in response.text
