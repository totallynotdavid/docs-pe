from __future__ import annotations

from typing import TYPE_CHECKING

from core.proxy.registry import PROVIDERS

from portal.domain.errors import Reason


if TYPE_CHECKING:
    from portal.domain.errors import PortalError


MESSAGES: dict[Reason, str] = {
    Reason.NOT_A_MEMBER: "no pertenece al equipo",
    Reason.LEADER_REQUIRED: "solo un líder del equipo puede continuar",
    Reason.SITE_ADMIN_REQUIRED: "solo la administración del sitio puede continuar",
    Reason.STEP_UP_REQUIRED: "confirma tu identidad con un código reciente para continuar",
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
    Reason.SECRET_UNREADABLE: "no se pudo descifrar el secreto almacenado",
    Reason.WORKER_NOT_AUTHORIZED: "el trabajador no está autorizado",
    Reason.WORKER_ID_INVALID: "el identificador del trabajador no es válido",
    Reason.INITIAL_TEAM_EXISTS: "la instalación ya tiene un equipo inicial",
    Reason.INITIAL_TEAM_MISMATCH: "el equipo inicial no coincide con el existente",
    Reason.TEAM_MISSING: "el equipo no existe",
    Reason.TEAM_NAME_LENGTH: "el nombre del equipo debe tener entre 1 y 120 caracteres",
    Reason.SLUG_INVALID: "el identificador debe usar minúsculas, números y guiones",
    Reason.EMAIL_INVALID: "el correo no tiene un formato válido",
    Reason.LABEL_LENGTH: "la etiqueta debe tener entre 1 y 120 caracteres",
    Reason.LABEL_TAKEN: "ya existe una conexión con ese nombre en este equipo",
    Reason.PASSWORD_TOO_SHORT: "la contraseña debe tener al menos {minimum} caracteres",
    Reason.ROLE_INVALID: "el rol seleccionado no es válido",
    Reason.LAST_LEADER: "el equipo debe conservar al menos una persona líder",
    Reason.INVITE_INVALID: "esta invitación no es válida o ya expiró",
    Reason.USER_LAST_LEADER: "es la única persona líder en: {teams}. Asigna otra persona líder en ese equipo antes de continuar",
    Reason.LAST_SITE_ADMIN: "la instalación debe conservar al menos una persona administradora activa",
    Reason.USER_CANNOT_DEACTIVATE_SELF: "no puedes desactivar ni eliminar tu propia cuenta",
    Reason.USER_HAS_HISTORY: "esta cuenta ya tiene actividad registrada; desactívala en lugar de eliminarla",
    Reason.WORKER_SOURCE_REQUIRED: "el trabajador debe declarar al menos una fuente",
    Reason.CSV_REQUIRED: "seleccione un archivo CSV",
    Reason.CSV_EXTENSION: "seleccione un archivo con extensión .csv",
    Reason.CSV_EMPTY: "el archivo CSV está vacío",
    Reason.CSV_TOO_LARGE: "el archivo CSV no puede superar los {limit_mb} MB",
    Reason.CSV_ENCODING: "el CSV debe usar codificación UTF-8",
    Reason.CSV_UNREADABLE: "no se pudo leer el archivo CSV",
    Reason.LAST_SECOND_FACTOR: "no puedes quitar tu único factor de seguridad",
    Reason.SETUP_EXPIRED: "la configuración expiró; vuelve a intentarlo",
    Reason.TOTP_CODE_INVALID: "el código no es válido",
    Reason.WEBAUTHN_VERIFICATION_FAILED: "no se pudo verificar la clave de acceso",
    Reason.PASSKEY_NOT_FOUND: "esa clave de acceso ya no existe",
    Reason.ENTRY_NOT_FOUND: "documento no encontrado en el equipo",
    Reason.INPUT_NOT_FOUND: "el archivo de la consulta ya no está disponible",
    Reason.GLOBAL_SEARCH_REQUIRED: "la búsqueda global no está habilitada para su equipo",
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
