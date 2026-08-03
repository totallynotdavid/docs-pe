from __future__ import annotations

from typing import TYPE_CHECKING

from fetch.proxy.registry import PROVIDERS

from portal.domain.errors import Reason


if TYPE_CHECKING:
    from portal.domain.errors import PortalError


MESSAGES: dict[Reason, str] = {
    Reason.NOT_A_MEMBER: "no pertenece al equipo",
    Reason.LEADER_REQUIRED: "solo un líder del equipo puede continuar",
    Reason.SITE_ADMIN_REQUIRED: "solo la administración del sitio puede continuar",
    Reason.CSRF_INVALID: "la verificación CSRF no es válida",
    Reason.TEAM_NOT_FOUND: "equipo no encontrado",
    Reason.JOB_NOT_FOUND: "proceso no encontrado en el equipo",
    Reason.USER_NOT_FOUND: "no se encontró una persona con ese correo",
    Reason.SOURCE_REQUIRED: "seleccione al menos una fuente",
    Reason.SOURCE_DUPLICATED: "las fuentes no se pueden repetir",
    Reason.SOURCE_NOT_ENABLED: "fuentes no habilitadas: {invalid}; use {allowed}",
    Reason.CREDENTIAL_REQUIRED: "el equipo necesita una credencial proxy activa",
    Reason.CREDENTIAL_WRONG_TEAM: "la credencial debe pertenecer al mismo equipo",
    Reason.CREDENTIAL_NOT_PENDING: "la credencial no está pendiente de validación",
    Reason.CREDENTIAL_STATE_INVALID: "el estado final de la credencial no es válido",
    Reason.PROXY_UNAVAILABLE: "el proveedor seleccionado no está disponible",
    Reason.PROXY_INVALID: "revise la configuración del proxy",
    Reason.PROXY_PREFLIGHT_FAILED: "no se pudo validar la conexión con el proveedor",
    Reason.INITIAL_TEAM_EXISTS: "la instalación ya tiene un equipo inicial",
    Reason.TEAM_MISSING: "el equipo no existe",
    Reason.TEAM_NAME_LENGTH: "el nombre del equipo debe tener entre 1 y 120 caracteres",
    Reason.SLUG_INVALID: "el identificador debe usar minúsculas, números y guiones",
    Reason.EMAIL_INVALID: "el correo no tiene un formato válido",
    Reason.LABEL_LENGTH: "la etiqueta debe tener entre 1 y 120 caracteres",
    Reason.PASSWORD_TOO_SHORT: "la contraseña debe tener al menos {minimum} caracteres",
    Reason.ROLE_INVALID: "el rol seleccionado no es válido",
    Reason.LAST_LEADER: "el equipo debe conservar al menos una persona líder",
    Reason.WORKER_SOURCE_REQUIRED: "el trabajador debe declarar al menos una fuente",
    Reason.CSV_REQUIRED: "seleccione un archivo CSV",
    Reason.CSV_EXTENSION: "seleccione un archivo con extensión .csv",
    Reason.CSV_EMPTY: "el archivo CSV está vacío",
    Reason.CSV_TOO_LARGE: "el archivo CSV no puede superar los {limit_mb} MB",
    Reason.CSV_ENCODING: "el CSV debe usar codificación UTF-8",
    Reason.CSV_UNREADABLE: "no se pudo leer el archivo CSV",
}

FIELD_LABELS: dict[str, str] = {
    "username": "Usuario",
    "password": "Contraseña",
    "gateway": "Puerta de enlace",
    "proxy_type": "Tipo de red",
    "country": "País de salida",
    "state": "Región",
    "city": "Ciudad",
    "asn": "ASN",
    "strict_off": "Desactivar modo estricto",
    "lifetime_minutes": "Duración de sesión (minutos)",
    "session_minutes": "Duración de sesión (minutos)",
}

CHOICE_LABELS: dict[str, str] = {
    "fr": "Francia",
    "fr_whitelist": "Francia (lista permitida)",
    "us": "Estados Unidos",
    "sg": "Singapur",
    "residential": "Residencial",
    "datacenter": "Centro de datos",
    "mix": "Mixta",
}

PROVIDER_LABELS: dict[str, str] = {
    "geonode": "GeoNode",
    "dataimpulse": "DataImpulse",
}


def message_for(error: PortalError) -> str:
    return MESSAGES[error.reason].format(**error.params)


def field_label(field_name: str) -> str:
    return FIELD_LABELS.get(field_name, field_name)


def choice_label(value: str) -> str:
    return CHOICE_LABELS.get(value, value)


def provider_label(name: str) -> str:
    return PROVIDER_LABELS.get(name, name)


def provider_names() -> tuple[str, ...]:
    return tuple(sorted(PROVIDERS))
