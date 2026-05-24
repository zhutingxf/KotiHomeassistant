"""Light platform for koti integration."""
import socket
import asyncio
from datetime import datetime
import logging
from homeassistant.core import callback
from homeassistant.components.light import LightEntity, ColorMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.core import HomeAssistant
from .const import DOMAIN, DEFAULT_TARGET_PORT

_LOGGER = logging.getLogger(__name__)

SERVICE_ID = "0001"
VERSION = "01.00"
ENCRYPTION = "0001"
SERIAL_NUM = "80000001"
LOGIN_NAME = "admin"
PASSWORD = "admin"

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up koti lights based on config entry."""
    entry_data  = hass.data[DOMAIN].get(entry.entry_id, {})
    devices = entry_data.get("data", {}).get("devices", [])
    entities = []
    for device in devices:
        if device.get("type") == 1:
            entities.append(KotiLight(device, entry, hass))
    async_add_entities(entities)


class KotiLight(LightEntity):
    """Representation of a koti light."""

    # 关键：明确声明支持的颜色模式（仅开/关）
    _attr_supported_color_modes = {ColorMode.ONOFF}
    _attr_color_mode = ColorMode.ONOFF

    def __init__(self, device: dict, entry: ConfigEntry, hass: HomeAssistant) -> None:
        self.hass = hass
        self._device = device
        self._entry = entry
        self._device_id = device["device_id"]
        self._control_channel = device.get("control_channel")
        self._name = device.get("name", f"Light {self._device_id}")

        self._attr_unique_id = f"{entry.entry_id}_light_{self._device_id}"
        self._attr_name = self._name
        self._attr_is_on = False
        self._attr_icon = "mdi:lightbulb"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._device_id)},
            name=self._name,
            manufacturer="Koti",
            model="Smart Light",
            sw_version="1.0",
        )
        # 注册实体
        if "entities" not in hass.data[DOMAIN]:
            hass.data[DOMAIN]["entities"] = {}
        if entry.entry_id not in hass.data[DOMAIN]["entities"]:
            hass.data[DOMAIN]["entities"][entry.entry_id] = {}
        hass.data[DOMAIN]["entities"][entry.entry_id][self._device_id] = self


    @property
    def extra_state_attributes(self):
        return {
            "device_id": self._device_id,
            "control_channel": self._control_channel,
        }

    async def async_turn_on(self, **kwargs):
        """Turn the light on."""
        await self._send_command("on")
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs):
        """Turn the light off."""
        await self._send_command("off")
        self._attr_is_on = False
        self.async_write_ha_state()

    async def _send_command(self, action: str):
        """通过 UDP 发送注册包和控制包到 koti 网关。"""
        host = self._entry.options.get("host") or self._entry.data.get("host")
        if not host:
            _LOGGER.error("No host configured for koti light %s", self._name)
            return

        value = "00FF" if action == "on" else "0000"
        timestamp = datetime.now().strftime("%Y/%m/%d %H:%M:%S")
        gateway_id = self._entry.data.get("gateway_id", "1001111113B00265")

        register_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
                            <PACKET>
                                <HEAD>
                                    <TIMESTAMP>{timestamp}</TIMESTAMP>
                                    <SERVICEID>{SERVICE_ID}</SERVICEID>
                                    <VERSION>V01.00</VERSION>
                                    <ENCRYTION>{ENCRYPTION}</ENCRYTION>
                                    <ID>{gateway_id}</ID>
                                    <SERIALNUM>{SERIAL_NUM}</SERIALNUM>
                                    <LOGINNAME>{LOGIN_NAME}</LOGINNAME>
                                    <PASSWORD>{PASSWORD}</PASSWORD>
                                </HEAD>
                                <BODY>
                                    <INSTP>REGISTERMAGICTOUCHREQ</INSTP>
                                    <NAME>koticonfig</NAME>
                                </BODY>
                            </PACKET>"""

        control_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
                            <PACKET>
                                <HEAD>
                                    <TIMESTAMP>{timestamp}</TIMESTAMP>
                                    <SERVICEID>{SERVICE_ID}</SERVICEID>
                                    <VERSION>{VERSION}</VERSION>
                                    <ENCRYTION>{ENCRYPTION}</ENCRYTION>
                                    <ID>{gateway_id}</ID>
                                    <SERIALNUM>{SERIAL_NUM}</SERIALNUM>
                                </HEAD>
                                <BODY>
                                    <INSTP>DEVICECONTROLREQ</INSTP>
                                    <CONTROLTYPE>0</CONTROLTYPE>
                                    <DEVICEID>{self._device_id}</DEVICEID>
                                    <VALUE>{value}</VALUE>
                                    <RFID>{self._control_channel}</RFID>
                                </BODY>
                            </PACKET>"""
        target_port_raw  = self.hass.data[DOMAIN].get("target_port", DEFAULT_TARGET_PORT)
        try:
            target_port = int(target_port_raw)
        except (TypeError, ValueError):
            target_port = DEFAULT_TARGET_PORT
        # 获取全局发送 socket
        udp_sock = self.hass.data[DOMAIN].get("udp_sock")
        if udp_sock is None:
            _LOGGER.error("UDP send socket not available")
            return
        # 目标端口（网关的监听端口，默认 16878）
        
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._send_udp, host, target_port, register_xml)
        await loop.run_in_executor(None, self._send_udp, host, target_port, control_xml)

    @staticmethod
    def _send_udp(host: str, port: int, message: str):
        """发送 UDP 数据报（同步）。"""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.sendto(message.encode("utf-8"), (host, port))
        except Exception as e:
            _LOGGER.error("UDP send error: %s", e)
        finally:
            sock.close()