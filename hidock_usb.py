"""Read-only USB client for HiDock recorders (H1 / H1E / P1).

Speaks the same vendor protocol hinotes.com uses over WebUSB. Only the
commands needed to list and download recordings are implemented; nothing
here can modify or delete anything on the device.
"""

from __future__ import annotations

import struct
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

import libusb_package
import usb.core
import usb.util

VENDOR_IDS = frozenset({0x10D6, 0x3887})
PRODUCT_IDS = frozenset({
    0xAF0C, 0xB00C, 0xAF0D, 0xB00D, 0xAF0E, 0xB00E, 0xAF0F,
    0x0100, 0x0101, 0x0102, 0x0103, 0x2040, 0x2041,
})

EP_OUT = 0x01
EP_IN = 0x82
SYNC = b"\x12\x34"
HEADER_LEN = 12

CMD_DEVICE_INFO = 1
CMD_FILE_LIST = 4
CMD_TRANSFER_FILE = 5
CMD_RECORDING_FILE = 18

_MONTHS = {m: i for i, m in enumerate(
    "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(), 1)}


class HiDockError(Exception):
    pass


class DeviceBusyError(HiDockError):
    """Another program (usually a hinotes.com browser tab) holds the device."""


@dataclass(frozen=True)
class DeviceFile:
    name: str
    size: int
    version: int
    signature: str
    recorded_at: datetime | None


def parse_recorded_at(name: str) -> datetime | None:
    """Recording start time from a device filename, e.g. 2025May12-114915-Rec44.hda."""
    stem = name.rsplit(".", 1)[0]
    try:
        if len(stem) >= 14 and stem[:14].isdigit():
            return datetime.strptime(stem[:14], "%Y%m%d%H%M%S")
        date_part, _, rest = stem.partition("-")
        clock = rest[:6]
        if len(clock) != 6 or not clock.isdigit():
            return None
        for year_len in (4, 2):
            year, month, day = date_part[:year_len], date_part[year_len:year_len + 3], date_part[year_len + 3:]
            if year.isdigit() and month in _MONTHS and day.isdigit():
                full_year = int(year) if year_len == 4 else 2000 + int(year)
                return datetime(full_year, _MONTHS[month], int(day),
                                int(clock[:2]), int(clock[2:4]), int(clock[4:6]))
    except ValueError:
        pass
    return None


def is_present() -> bool:
    return _find() is not None


def _find():
    for dev in libusb_package.find(find_all=True):
        if dev.idVendor in VENDOR_IDS and dev.idProduct in PRODUCT_IDS:
            return dev
    return None


class HiDock:
    def __init__(self) -> None:
        self._dev = None
        self._seq = 0
        self._buf = bytearray()

    def __enter__(self) -> "HiDock":
        dev = _find()
        if dev is None:
            raise HiDockError("HiDock device not found on USB")
        try:
            try:
                dev.get_active_configuration()
            except usb.core.USBError:
                dev.set_configuration()
            usb.util.claim_interface(dev, 0)
        except (usb.core.USBError, NotImplementedError) as exc:
            usb.util.dispose_resources(dev)
            raise DeviceBusyError(
                f"Could not open the HiDock ({exc}). Close any hinotes.com tab and replug."
            ) from exc
        self._dev = dev
        self._drain()
        return self

    def __exit__(self, *exc) -> None:
        if self._dev is not None:
            try:
                usb.util.release_interface(self._dev, 0)
            except usb.core.USBError:
                pass
            usb.util.dispose_resources(self._dev)
            self._dev = None

    @property
    def model(self) -> str:
        return self._dev.product or "HiDock"

    # ── framing ──────────────────────────────────────────────────────────

    def _drain(self) -> None:
        self._buf.clear()
        for _ in range(50):
            try:
                if not len(self._dev.read(EP_IN, 32768, timeout=30)):
                    return
            except usb.core.USBError:
                return

    def _send(self, cmd: int, body: bytes = b"") -> int:
        self._seq = (self._seq + 1) & 0xFFFFFFFF
        packet = SYNC + struct.pack(">HII", cmd, self._seq, len(body)) + body
        self._dev.write(EP_OUT, packet, timeout=5000)
        return self._seq

    def _next_packet(self, timeout_s: float) -> tuple[int, int, bytes] | None:
        """Next complete (cmd, seq, body) packet, or None if none arrives in time."""
        deadline = time.monotonic() + timeout_s
        while True:
            start = self._buf.find(SYNC)
            if start > 0:
                del self._buf[:start]
            elif start < 0 and len(self._buf) > 1:
                del self._buf[:-1]
            if len(self._buf) >= HEADER_LEN:
                cmd, seq, raw_len = struct.unpack_from(">HII", self._buf, 2)
                total = HEADER_LEN + (raw_len & 0x00FFFFFF) + (raw_len >> 24)
                if len(self._buf) >= total:
                    body = bytes(self._buf[HEADER_LEN:HEADER_LEN + (raw_len & 0x00FFFFFF)])
                    del self._buf[:total]
                    return cmd, seq, body
            if time.monotonic() >= deadline:
                return None
            try:
                self._buf += self._dev.read(EP_IN, 65536, timeout=250)
            except usb.core.USBTimeoutError:
                continue

    def _request(self, cmd: int, body: bytes = b"", timeout_s: float = 5) -> bytes:
        self._buf.clear()
        seq = self._send(cmd, body)
        deadline = time.monotonic() + timeout_s
        while (remaining := deadline - time.monotonic()) > 0:
            packet = self._next_packet(remaining)
            if packet and packet[0] == cmd and packet[1] == seq:
                return packet[2]
        raise HiDockError(f"No reply to command {cmd}")

    # ── commands ─────────────────────────────────────────────────────────

    def device_info(self) -> dict:
        body = self._request(CMD_DEVICE_INFO)
        serial = body[4:20].split(b"\x00")[0].decode("ascii", "ignore").strip()
        return {"firmware": ".".join(str(b) for b in body[1:4]), "serial": serial}

    def recording_in_progress(self) -> str | None:
        """Name of the file the device says it is recording to (may also be the last one)."""
        try:
            body = self._request(CMD_RECORDING_FILE, timeout_s=3)
        except HiDockError:
            return None
        name = body.split(b"\x00")[0].decode("ascii", "ignore").strip()
        return name or None

    def list_files(self) -> list[DeviceFile]:
        self._buf.clear()
        self._send(CMD_FILE_LIST)
        data = bytearray()
        quiet = 0
        while quiet < 4:
            packet = self._next_packet(1.5)
            if packet is None:
                quiet += 1
                continue
            cmd, _, body = packet
            if cmd != CMD_FILE_LIST:
                continue
            if not body:
                break
            quiet = 0
            data += body
            files, expected = _parse_file_list(data)
            if expected is not None and len(files) >= expected:
                return files
        files, expected = _parse_file_list(data)
        if expected is not None and len(files) < expected:
            raise HiDockError(f"Incomplete file list: {len(files)}/{expected}")
        return files

    def download(
        self,
        file: DeviceFile,
        dest: Path,
        progress: Callable[[int, int], None] | None = None,
    ) -> None:
        """Stream one recording to dest (written via a .part file, renamed on success)."""
        part = dest.with_name(dest.name + ".part")
        self._buf.clear()
        self._send(CMD_TRANSFER_FILE, file.name.encode("ascii"))
        received = 0
        try:
            with part.open("wb") as out:
                while received < file.size:
                    packet = self._next_packet(20)
                    if packet is None:
                        raise HiDockError(
                            f"Transfer of {file.name} stalled at {received}/{file.size} bytes")
                    cmd, _, body = packet
                    if cmd != CMD_TRANSFER_FILE or not body:
                        continue
                    out.write(body)
                    received += len(body)
                    if progress:
                        progress(received, file.size)
            if received != file.size:
                raise HiDockError(f"{file.name}: got {received} bytes, expected {file.size}")
            part.replace(dest)
        except BaseException:
            part.unlink(missing_ok=True)
            self._drain()
            raise


def _parse_file_list(data: bytes | bytearray) -> tuple[list[DeviceFile], int | None]:
    pos = 0
    expected = None
    if len(data) >= 6 and data[0] == 0xFF and data[1] == 0xFF:
        expected = struct.unpack_from(">I", data, 2)[0]
        pos = 6
    files: list[DeviceFile] = []
    while pos + 4 <= len(data):
        version = data[pos]
        name_len = int.from_bytes(data[pos + 1:pos + 4], "big")
        end = pos + 4 + name_len + 4 + 6 + 16
        if end > len(data):
            break
        name_end = pos + 4 + name_len
        name = bytes(data[pos + 4:name_end]).rstrip(b"\x00").decode("ascii", "ignore")
        size = struct.unpack_from(">I", data, name_end)[0]
        signature = bytes(data[name_end + 10:end]).hex()
        files.append(DeviceFile(name, size, version, signature, parse_recorded_at(name)))
        pos = end
    return files, expected


if __name__ == "__main__":
    with HiDock() as dock:
        print(dock.model, dock.device_info())
        print("recording:", dock.recording_in_progress())
        for f in dock.list_files():
            print(f"{f.name:40} v{f.version} {f.size / 1e6:8.2f} MB  {f.recorded_at}")
