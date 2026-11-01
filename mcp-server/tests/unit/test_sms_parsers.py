"""Parsers tolerantes: estados, ids externos y colecciones de detalles."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from src.providers.oneglobal_client import (
    normalize_status,
    parse_external_task_id,
    parse_sms_collection,
    parse_status_payload,
)
from src.utils.time_utils import format_api_date, parse_api_date


def test_normalizacion_de_estados_tabla_completa() -> None:
    delivered = ["DELIVERED", "received", "Acknowledged", "COMPLETED", "Success", "sent delivered"]
    for raw in delivered:
        assert normalize_status(raw) == "delivered", raw
    for raw in ["SENT", "Submitted", "ACCEPTED", "in progress", "Processing", "queued"]:
        assert normalize_status(raw) == "sent", raw
    for raw in ["pending", "CREATED", "New", "Scheduled"]:
        assert normalize_status(raw) == "pending", raw
    for raw in ["FAILED", "Error", "REJECTED", "undelivered", "Expired", "not delivered"]:
        assert normalize_status(raw) == "failed", raw
    for raw in ["CANCELLED", "canceled", "Stopped"]:
        assert normalize_status(raw) == "cancelled", raw
    assert normalize_status("estado raro") == "unknown"
    assert normalize_status(None) == "unknown"
    assert normalize_status("") == "unknown"


def test_id_externo_desde_url_con_doble_barra() -> None:
    raw = {"resourceURL": "https://iot.truphone.com/api/v2.0/sims/send_sms//status/12345/", "detail": "OK"}
    assert parse_external_task_id(raw) == "12345"


def test_id_externo_desde_url_normal() -> None:
    raw = {"resourceURL": "https://iot.truphone.com/api/v2.0/sims/send_sms/status/12345/"}
    assert parse_external_task_id(raw) == "12345"


def test_id_externo_directo_y_alias() -> None:
    assert parse_external_task_id({"id": "A"}) == "A"
    assert parse_external_task_id({"sms_id": "B"}) == "B"
    assert parse_external_task_id({"taskId": "C"}) == "C"
    assert parse_external_task_id({"data": {"id": "D"}}) == "D"


def test_id_externo_ausente_devuelve_none() -> None:
    assert parse_external_task_id({"detail": "OK"}) is None
    assert parse_external_task_id(None) is None


def test_estado_con_aliases_y_envoltorio() -> None:
    raw = {"data": {"deliveryReport": [{"iccid": "89…", "deliveryStatus": "DELIVERED"}]}}
    status = parse_status_payload(raw)
    assert status.status == "delivered"


def test_sin_reporte_de_entrega_no_es_entregado() -> None:
    status = parse_status_payload({"id": "1", "dateSubmitted": "2026-11-01 14:02:11+00:00"})
    assert status.status == "unknown"
    assert status.raw_status is None


def test_coleccion_array_plano() -> None:
    raw = [{"iccid": "8944474400001005750", "content": "  web_connect", "dateSubmitted": "2026-11-01 14:02:11"}]
    details = parse_sms_collection(raw)
    assert len(details) == 1
    assert details[0].content == "  web_connect"


def test_coleccion_envueltos_results_y_data() -> None:
    item = {"iccid": "8", "message": "hola", "startDate": "2026-11-01T14:02:11Z"}
    for wrapper in ({"results": [item]}, {"data": [item]}, {"deliveryReport": [item]}):
        details = parse_sms_collection(wrapper)
        assert details[0].content == "hola"
        assert details[0].date_submitted == "2026-11-01T14:02:11Z"


def test_iccid_del_primer_elemento_delivery_report() -> None:
    raw = [{"deliveryReport": [{"iccid": "8944474400001005750", "deliveryStatus": "DELIVERED"}]}]
    assert parse_sms_collection(raw)[0].iccid == "8944474400001005750"


def test_formato_de_fecha_para_la_api() -> None:
    moment = datetime(2026, 11, 1, 14, 2, 11, tzinfo=UTC)
    assert format_api_date(moment) == "2026-11-01 14:02:11"


def test_parse_fechas_de_la_api() -> None:
    parsed = parse_api_date("2026-10-31 14:02:11+00:00")
    assert parsed == datetime(2026, 10, 31, 14, 2, 11, tzinfo=UTC)
    assert parse_api_date("2026-10-31 14:02:11") == datetime(2026, 10, 31, 14, 2, 11, tzinfo=UTC)
    assert parse_api_date(None) is None
    assert parse_api_date("basura") is None


def test_ventana_de_busqueda_resta_margen_de_reloj() -> None:
    created = datetime(2026, 11, 1, 14, 10, 0, tzinfo=UTC)
    since = created - timedelta(minutes=10)
    assert format_api_date(since) == "2026-11-01 14:00:00"
