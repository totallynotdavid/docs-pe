from __future__ import annotations

from portal.web.sse import sse_event


def test_every_line_of_a_multi_line_fragment_gets_a_data_prefix() -> None:
    """A bare newline would end the event early and truncate the fragment."""
    framed = sse_event(
        event_id=7, event="progreso", data="<div>\n  <p>hola</p>\n</div>"
    )

    assert framed == (
        "id: 7\nevent: progreso\ndata: <div>\ndata:   <p>hola</p>\ndata: </div>\n\n"
    )


def test_an_event_without_an_id_omits_the_id_line() -> None:
    assert sse_event(event="progreso", data="hola") == "event: progreso\ndata: hola\n\n"
