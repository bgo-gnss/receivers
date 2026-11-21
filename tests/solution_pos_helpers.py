"""Synthetic PBO .pos builders for the solution-health tests (no real data)."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

MJD0 = dt.date(1858, 11, 17)

HEADER_FIELDS = (
    "Start Field Description\n"
    "YYYYMMDD      Year, month, day for the given position epoch\n"
    "End Field Description\n"
    "*YYYYMMDD HHMMSS JJJJJ.JJJJ         X             Y             Z  ...  Soln"
)


def header(station: str = "AFST", frame: str = "IGb14", sid: str | None = None) -> str:
    sid_line = "" if sid == "" else f"4-character ID: {sid or station}\n"
    return (
        f"PBO Station Position Time Series. Reference Frame : {frame}\n"
        "Format Version: 1.1.1\n"
        f"{sid_line}"
        f"Station name  : {station}_GPS        \n"
        "First Epoch   : 20210304 115900\n"
        "Last Epoch    : 20261120 115900\n"
        "Release Date  : 20261121 164524\n"
        "XYZ Reference position :   2598588.25855 -1057242.66806  5708965.41087 (IGb14)\n"
        "NEU Reference position :    63.9821764714  337.8609171791  135.49134 (IGb14/WGS84)\n"
        f"{HEADER_FIELDS}\n"
    )


def row(
    day: dt.date,
    *,
    time: str = "115900",
    mjd: str | None = None,
    x: str = "2598588.29093",
    y: str = "-1057242.58535",
    z: str = "5708965.36107",
    sigma_x: str = "0.00290",
    dn: str = "-0.02076",
    soln: str = "24hrs",
    extra: str = "",
) -> str:
    if mjd is None:
        mjd = f"{(day - MJD0).days + 0.4993:10.4f}"
    return (
        f" {day:%Y%m%d} {time} {mjd}  {x} {y}  {z}  {sigma_x}  0.00197  0.00568"
        " -0.362  0.656 -0.370       63.9821762850  337.8609189930  135.44607"
        f"    {dn}   0.08858  -0.04527    0.00212  0.00175  0.00608  0.002"
        f"  0.152  0.026 {soln}{extra}"
    )


def write_pos(
    path: Path,
    days: list[dt.date],
    *,
    station: str = "AFST",
    rows: list[str] | None = None,
    **header_kw: str,
) -> Path:
    body = rows if rows is not None else [row(d) for d in days]
    path.write_text(header(station, **header_kw) + "\n".join(body) + "\n")
    return path
