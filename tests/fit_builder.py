"""Minimal FIT binary encoder used to build synthetic fixtures in tests.

Only the subset of the FIT protocol needed by the parser is implemented:
a 14-byte header, little-endian definition/data messages for `file_id`,
`device_info`, `record`, `lap` and `session`, and the trailing CRC16.
No real device data is embedded.
"""

import struct
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

FIT_EPOCH = datetime(1989, 12, 31, tzinfo=timezone.utc)

UINT8 = 0x02
UINT16 = 0x84
UINT32 = 0x86
SINT32 = 0x85
UINT32Z = 0x8C
ENUM = 0x00

_FMT = {UINT8: "B", ENUM: "B", UINT16: "H", UINT32: "I", SINT32: "i", UINT32Z: "I"}
_SIZE = {UINT8: 1, ENUM: 1, UINT16: 2, UINT32: 4, SINT32: 4, UINT32Z: 4}

MSG_FILE_ID = 0
MSG_SESSION = 18
MSG_LAP = 19
MSG_RECORD = 20
MSG_DEVICE_INFO = 23
MSG_ACTIVITY = 34

SPORT_RUNNING = 1
SPORT_CYCLING = 2
SUB_SPORT_TRAIL = 3
SUB_SPORT_GENERIC = 0
MANUFACTURER_GARMIN = 1

_CRC_TABLE = [
    0x0000, 0xCC01, 0xD801, 0x1400, 0xF001, 0x3C00, 0x2800, 0xE401,
    0xA001, 0x6C00, 0x7800, 0xB401, 0x5000, 0x9C01, 0x8801, 0x4400,
]


def fit_crc(data: bytes, crc: int = 0) -> int:
    for byte in data:
        tmp = _CRC_TABLE[crc & 0xF]
        crc = (crc >> 4) & 0x0FFF
        crc = crc ^ tmp ^ _CRC_TABLE[byte & 0xF]
        tmp = _CRC_TABLE[crc & 0xF]
        crc = (crc >> 4) & 0x0FFF
        crc = crc ^ tmp ^ _CRC_TABLE[(byte >> 4) & 0xF]
    return crc


def to_fit_time(dt: datetime) -> int:
    return int((dt - FIT_EPOCH).total_seconds())


def degrees_to_semicircles(deg: float) -> int:
    return int(deg * (2**31) / 180.0)


@dataclass
class FitMessageDef:
    global_num: int
    fields: list[tuple[int, int]]  # (field_def_num, base_type)
    local_num: int = 0


class FitWriter:
    def __init__(self) -> None:
        self._body = bytearray()
        self._defined: dict[int, FitMessageDef] = {}

    def define(self, msg: FitMessageDef) -> None:
        header = 0x40 | (msg.local_num & 0x0F)
        out = bytearray([header, 0, 0])
        out += struct.pack("<HB", msg.global_num, len(msg.fields))
        for def_num, base_type in msg.fields:
            out += bytes([def_num, _SIZE[base_type], base_type])
        self._body += out
        self._defined[msg.local_num] = msg

    def data(self, local_num: int, values: list[int]) -> None:
        msg = self._defined[local_num]
        assert len(values) == len(msg.fields)
        out = bytearray([local_num & 0x0F])
        for (_, base_type), value in zip(msg.fields, values, strict=True):
            out += struct.pack("<" + _FMT[base_type], value)
        self._body += out

    def build(self) -> bytes:
        body = bytes(self._body)
        header_wo_crc = struct.pack("<BBHI4s", 14, 0x20, 2132, len(body), b".FIT")
        header = header_wo_crc + struct.pack("<H", fit_crc(header_wo_crc))
        return header + body + struct.pack("<H", fit_crc(body))


@dataclass
class SyntheticActivity:
    """Parameters for a synthetic single-session running FIT file."""

    start: datetime = field(
        default_factory=lambda: datetime(2024, 3, 10, 7, 30, tzinfo=timezone.utc)
    )
    duration_s: int = 1800
    distance_m: float = 6000.0
    sample_interval_s: int = 60
    hr_start: int = 120
    hr_end: int = 160
    ascent_m: int = 150
    sport: int = SPORT_RUNNING
    sub_sport: int = SUB_SPORT_GENERIC
    serial_number: int = 3_900_001_234
    product: int = 3990
    manufacturer: int = MANUFACTURER_GARMIN
    lat: float = -33.45
    lon: float = -70.66
    include_hr: bool = True
    cadence_rpm: int = 85  # FIT cadence is per-leg for foot sports (=> 170 spm)
    utc_offset_s: int | None = -3 * 3600


def build_fit(spec: SyntheticActivity | None = None) -> bytes:
    spec = spec or SyntheticActivity()
    w = FitWriter()
    t0 = to_fit_time(spec.start)

    w.define(
        FitMessageDef(
            MSG_FILE_ID,
            [(0, ENUM), (1, UINT16), (2, UINT16), (3, UINT32Z), (4, UINT32)],
            local_num=0,
        )
    )
    w.data(0, [4, spec.manufacturer, spec.product, spec.serial_number, t0])

    w.define(
        FitMessageDef(
            MSG_DEVICE_INFO,
            [(253, UINT32), (2, UINT16), (4, UINT16), (3, UINT32Z)],
            local_num=1,
        )
    )
    w.data(1, [t0, spec.manufacturer, spec.product, spec.serial_number])

    record_fields = [
        (253, UINT32),
        (0, SINT32),
        (1, SINT32),
        (5, UINT32),
        (78, UINT32),
        (73, UINT32),
        (4, UINT8),
    ]
    if spec.include_hr:
        record_fields.append((3, UINT8))
    w.define(FitMessageDef(MSG_RECORD, record_fields, local_num=2))

    n = spec.duration_s // spec.sample_interval_s
    speed_mps = spec.distance_m / spec.duration_s
    for i in range(n + 1):
        frac = i / max(n, 1)
        dist = spec.distance_m * frac
        alt = 500.0 + spec.ascent_m * frac
        hr = int(spec.hr_start + (spec.hr_end - spec.hr_start) * frac)
        values = [
            t0 + i * spec.sample_interval_s,
            degrees_to_semicircles(spec.lat + 0.0001 * i),
            degrees_to_semicircles(spec.lon + 0.0001 * i),
            int(dist * 100),
            int((alt + 500) * 5),
            int(speed_mps * 1000),
            spec.cadence_rpm,
        ]
        if spec.include_hr:
            values.append(hr)
        w.data(2, values)

    w.define(
        FitMessageDef(
            MSG_LAP,
            [
                (253, UINT32),
                (2, UINT32),
                (7, UINT32),
                (8, UINT32),
                (9, UINT32),
                (21, UINT16),
                (22, UINT16),
                (25, ENUM),
            ],
            local_num=3,
        )
    )
    w.data(
        3,
        [
            t0 + spec.duration_s,
            t0,
            spec.duration_s * 1000,
            spec.duration_s * 1000,
            int(spec.distance_m * 100),
            spec.ascent_m,
            0,
            spec.sport,
        ],
    )

    session_fields = [
        (253, UINT32),
        (2, UINT32),
        (7, UINT32),
        (8, UINT32),
        (9, UINT32),
        (5, ENUM),
        (6, ENUM),
        (22, UINT16),
        (23, UINT16),
        (18, UINT8),
    ]
    session_values = [
        t0 + spec.duration_s,
        t0,
        spec.duration_s * 1000,
        spec.duration_s * 1000,
        int(spec.distance_m * 100),
        spec.sport,
        spec.sub_sport,
        spec.ascent_m,
        0,
        spec.cadence_rpm,
    ]
    if spec.include_hr:
        session_fields += [(16, UINT8), (17, UINT8)]
        session_values += [(spec.hr_start + spec.hr_end) // 2, spec.hr_end]
    w.define(FitMessageDef(MSG_SESSION, session_fields, local_num=4))
    w.data(4, session_values)

    if spec.utc_offset_s is not None:
        w.define(
            FitMessageDef(
                MSG_ACTIVITY,
                [(253, UINT32), (5, UINT32), (1, UINT16), (2, ENUM), (3, ENUM), (4, ENUM)],
                local_num=5,
            )
        )
        t_end = t0 + spec.duration_s
        w.data(5, [t_end, t_end + spec.utc_offset_s, 1, 0, 26, 1])

    return w.build()


def shifted(spec: SyntheticActivity, seconds: int) -> SyntheticActivity:
    """Return a copy of `spec` whose start is shifted by `seconds`."""
    return SyntheticActivity(
        **{**spec.__dict__, "start": spec.start + timedelta(seconds=seconds)}
    )
