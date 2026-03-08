import io

from goodwe.protocol import ProtocolResponse
from goodwe.sensor import (
    DAY_NAMES,
    MONTH_NAMES,
    Schedule,
    ScheduleType,
    decode_day_of_week,
    decode_months,
)


def read_byte(buffer: ProtocolResponse, offset: int = None) -> int:
    """Retrieve single byte (signed int) value from buffer"""
    if offset is not None:
        buffer.seek(offset)
    return int.from_bytes(buffer.read(1), byteorder="big", signed=True)


def encode_byte(data: int) -> bytes:
    """Encode single byte (signed int)"""
    return data.to_bytes(1, byteorder="big", signed=True)


def read_bytes2(buffer: ProtocolResponse, offset: int = None, undef: int = None) -> int:
    """Retrieve 2 byte (unsigned int) value from buffer"""
    if offset is not None:
        buffer.seek(offset)
    value = int.from_bytes(buffer.read(2), byteorder="big", signed=False)
    return undef if value == 0xffff else value


def encode_bytes2(data: int) -> bytes:
    """Encode 2 byte (unsigned int)"""
    return data.to_bytes(2, byteorder="big", signed=False)


def read_bytes2_signed(buffer: ProtocolResponse, offset: int = None) -> int:
    """Retrieve 2 byte (signed int) value from buffer"""
    if offset is not None:
        buffer.seek(offset)
    return int.from_bytes(buffer.read(2), byteorder="big", signed=True)


def encode_bytes2_signed(data: int) -> bytes:
    """Encode 2 byte (signed int)"""
    return data.to_bytes(2, byteorder="big", signed=True)


def read_value(self: Schedule, data: ProtocolResponse) -> Schedule:
    self.start_h = read_byte(data)
    if (self.start_h < 0 or self.start_h > 23) and self.start_h != 48 and self.start_h != -1:
        raise ValueError(f"{self.id_}: start_h value {self.start_h} out of range.")
    self.start_m = read_byte(data)
    if (self.start_m < 0 or self.start_m > 59) and self.start_m != -1:
        raise ValueError(f"{self.id_}: start_m value {self.start_m} out of range.")
    self.end_h = read_byte(data)
    if (self.end_h < 0 or self.end_h > 23) and self.end_h != 48 and self.end_h != -1:
        raise ValueError(f"{self.id_}: end_h value {self.end_h} out of range.")
    self.end_m = read_byte(data)
    if (self.end_m < 0 or self.end_m > 59) and self.end_m != -1:
        raise ValueError(f"{self.id_}: end_m value {self.end_m} out of range.")
    self.on_off = read_byte(data)
    self.schedule_type = ScheduleType.detect_schedule_type(self.on_off)
    self.day_bits = read_byte(data)
    self.days = decode_day_of_week(self.day_bits)
    self.power = read_bytes2_signed(data)  # negative=charge, positive=discharge
    if not self.schedule_type.is_in_range(self.power):
        raise ValueError(f"{self.id_}: power value {self.power} out of range.")
    self.soc = read_bytes2_signed(data)
    if self.soc < 0 or self.soc > 100:
        raise ValueError(f"{self.id_}: SoC value {self.soc} out of range.")
    self.month_bits = read_bytes2_signed(data)
    self.months = decode_months(self.month_bits)
    return self


def encode_schedule(self: Schedule) -> bytes:
    output: io.BytesIO = io.BytesIO()
    output.write(encode_byte(self.start_h))
    output.write(encode_byte(self.start_m))
    output.write(encode_byte(self.end_h))
    output.write(encode_byte(self.end_m))
    output.write(encode_byte(self.on_off))  # TODO: verify with schedule_type
    # self.schedule_type = ScheduleType.detect_schedule_type(self.on_off)
    output.write(encode_byte(self.day_bits))
    # self.days = decode_day_of_week(self.day_bits)  # TODO: this should be a property
    output.write(encode_bytes2_signed(self.power))  # negative=charge, positive=discharge
    output.write(encode_bytes2_signed(self.soc))
    output.write(encode_bytes2_signed(self.month_bits))
    # self.months = decode_months(self.month_bits)  # this should be a property
    return output.getvalue()


def encode_day_of_week(days: str | list[int]) -> int:
    """Encode days to day_bits. days: 'Mon-Sun' | 'Mon,Tue,Wed' | list of 0..6 (Sun=0)."""
    if isinstance(days, list):
        bits = 0
        for d in days:
            if 0 <= d <= 6:
                bits |= 1 << d
        return bits
    s = (days or "").strip()
    if not s or s.lower() == "none":
        return 0
    if "sun" in s.lower() and "-" in s:
        return 127  # Mon-Sun / all days
    bits = 0
    for part in s.replace(" ", "").split(","):
        part = part.strip()
        for i, name in enumerate(DAY_NAMES):
            if name.lower() == part.lower():
                bits |= 1 << i
                break
    return bits


def encode_months(months: str | list[int] | None) -> int:
    """Encode months to month_bits. months: None | '' | 'Jan,Feb' | list of 1..12. 0 = all/none."""
    if months is None or (isinstance(months, str) and not (months or "").strip()):
        return 0
    if isinstance(months, list):
        bits = 0
        for m in months:
            if 1 <= m <= 12:
                bits |= 1 << (m - 1)
        return bits
    bits = 0
    for part in months.replace(" ", "").split(","):
        part = part.strip()
        for i, name in enumerate(MONTH_NAMES):
            if name.lower() == part.lower():
                bits |= 1 << i
                break
    return bits


def encode_eco_v1(
    start_h: int,
    start_m: int,
    end_h: int,
    end_m: int,
    power: int,
    day_bits: int,
    on_off: int = -1,
) -> bytes:
    """Encode EcoMode V1 slot (8 bytes). power: negative=charge, positive=discharge %."""
    output: io.BytesIO = io.BytesIO()
    output.write(encode_byte(start_h))
    output.write(encode_byte(start_m))
    output.write(encode_byte(end_h))
    output.write(encode_byte(end_m))
    output.write(encode_bytes2_signed(power))
    output.write(encode_byte(on_off))
    output.write(encode_byte(day_bits))
    return output.getvalue()
