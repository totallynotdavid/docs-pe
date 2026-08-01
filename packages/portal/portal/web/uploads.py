from __future__ import annotations

import csv
import io

from pathlib import Path

from fastapi import UploadFile

from portal.domain.models import InputLine


MAX_CSV_UPLOAD_BYTES = 10 * 1024 * 1024


def csv_input_lines(content: bytes) -> tuple[InputLine, ...]:
    """Read one document per row from the first column of an UTF-8 CSV file."""
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        msg = "el CSV debe usar codificación UTF-8"
        raise ValueError(msg) from error

    try:
        rows = csv.reader(io.StringIO(text, newline=""))
        return tuple(
            InputLine(ordinal, row[0].strip())
            for ordinal, row in enumerate(rows, start=1)
            if row and row[0].strip()
        )
    except csv.Error as error:
        msg = "no se pudo leer el archivo CSV"
        raise ValueError(msg) from error


async def read_csv_upload(input_file: UploadFile | None) -> tuple[str, bytes]:
    """Validate an uploaded CSV and return its name, stripped of directories."""
    if input_file is None or not input_file.filename:
        raise ValueError("seleccione un archivo CSV")
    uploaded_filename = Path(input_file.filename).name
    if not uploaded_filename.lower().endswith(".csv"):
        raise ValueError("seleccione un archivo con extensión .csv")
    content = await input_file.read(MAX_CSV_UPLOAD_BYTES + 1)
    if len(content) > MAX_CSV_UPLOAD_BYTES:
        raise ValueError("el archivo CSV no puede superar los 10 MB")
    if not content:
        raise ValueError("el archivo CSV está vacío")
    return uploaded_filename, content
