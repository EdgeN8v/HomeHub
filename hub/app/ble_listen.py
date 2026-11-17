"""Reads the Xiaomi BLE sensors' own advertisements (MiBeacon) off the air and decrypts them locally with the per-device
bind key the owner already has (BLE_DATA.beaconkey in mi_tokens.json) — no Xiaomi cloud, no gateway, no Mijia app,
and nothing taken from the vendor's app: just the device's own broadcast, opened with the owner's own key.

The radio work and the frame decoding are the well-trodden ones Home Assistant uses:
  * bleak listens on the host's Bluetooth adapter through BlueZ over D-Bus (no raw sockets, no extra privileges)
  * xiaomi_ble turns one FE95 advertisement + the bind key into sensor values (temperature, humidity, illuminance,
    occupancy, door open/close, leak, smoke, battery, button ...)

It only reads. Door/motion/leak/smoke/button come as events, so the listener runs non-stop and keeps the last value
of each; a device that has not broadcast since start simply has no value yet. This mirrors local_bridge/local_reader
for the Wi-Fi devices: the hub's own copy of the state, taken from the device instead of the cloud.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

log = logging.getLogger("ble_listen")

FE95 = "0000fe95-0000-1000-8000-00805f9b34fb"


def load_ble(path: Path) -> dict[str, dict]:
    """mac (upper) -> {did, model, name, key} for every BLE device of the account dump that carries a bind key."""
    out: dict[str, dict] = {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return out
    for srv in data:
        for home in srv.get("homes", []):
            for d in home.get("devices", []):
                bd = d.get("BLE_DATA") or {}
                key, mac = bd.get("beaconkey"), (d.get("mac") or "").upper()
                if key and mac:
                    out[mac] = {"did": str(d.get("did", "")), "model": d.get("model", ""),
                                "name": d.get("name", ""), "key": key}
    return out


class BleListener:
    """Passive MiBeacon listener. Start run() as a task; read the latest values with snapshot()."""

    def __init__(self, data: Path):
        self.devs = load_ble(data / "mi_tokens.json")
        self.values: dict[str, dict] = {}        # mac -> {sensor_key: {"value", "unit", "at"}}
        self._parsers: dict[str, object] = {}
        self.running = False
        self.err = ""

    def _parser(self, mac: str):
        from xiaomi_ble import XiaomiBluetoothDeviceData
        p = self._parsers.get(mac)
        if p is None:
            p = self._parsers[mac] = XiaomiBluetoothDeviceData(bindkey=bytes.fromhex(self.devs[mac]["key"]))
        return p

    def _on_adv(self, dev, adv):
        mac = dev.address.upper()
        info = self.devs.get(mac)
        if not info or FE95 not in adv.service_data:
            return
        from bluetooth_sensor_state_data import BluetoothServiceInfo
        si = BluetoothServiceInfo(name=adv.local_name or info["name"], address=dev.address, rssi=adv.rssi,
                                  manufacturer_data=adv.manufacturer_data, service_data=adv.service_data,
                                  service_uuids=adv.service_uuids, source="hci0")
        try:
            upd = self._parser(mac).update(si)
        except Exception as e:                    # a frame we cannot open is simply skipped, like a lost packet
            log.debug("cannot parse %s: %s", info["name"], e)
            return
        now = time.time()
        rec = self.values.setdefault(mac, {})
        for key, s in upd.entity_values.items():
            if s.native_value is not None:
                rec[str(key.key)] = {"value": s.native_value,
                                     "unit": getattr(s, "native_unit_of_measurement", None), "at": now}

    async def run(self):
        """Listen forever; a scanner that dies (adapter reset, BlueZ restart) is brought back up."""
        if not self.devs:
            self.err = "no BLE devices with a bind key in mi_tokens.json"
            log.warning(self.err)
            return
        try:
            from bleak import BleakScanner
        except Exception as e:                    # no bluetooth stack in this build: stay off, do not crash the hub
            self.err = f"bleak unavailable: {e}"
            log.warning("BLE listener off (%s)", e)
            return
        while True:
            scanner = BleakScanner(detection_callback=self._on_adv)
            try:
                await scanner.start()
                self.running, self.err = True, ""
                log.info("BLE listener up, watching %d device(s)", len(self.devs))
                while True:
                    await asyncio.sleep(3600)
            except asyncio.CancelledError:
                try:
                    await scanner.stop()
                except Exception:
                    pass
                self.running = False
                raise
            except Exception as e:
                self.running, self.err = False, str(e)
                log.warning("BLE scanner restarting: %s", e)
                try:
                    await scanner.stop()
                except Exception:
                    pass
                await asyncio.sleep(10)

    def snapshot(self) -> dict:
        devices = []
        for mac, info in self.devs.items():
            rec = self.values.get(mac, {})
            devices.append({"did": info["did"], "mac": mac, "name": info["name"], "model": info["model"],
                            "values": {k: v["value"] for k, v in rec.items()},
                            "at": max((v["at"] for v in rec.values()), default=0)})
        devices.sort(key=lambda d: (d["at"] == 0, d["name"]))
        return {"ok": self.running and not self.err, "err": self.err, "total": len(self.devs),
                "live": sum(1 for d in devices if d["at"]), "devices": devices}
