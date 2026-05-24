import socket
import asyncio
import logging
from datetime import datetime, timedelta
import xml.etree.ElementTree as ET
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from .const import DOMAIN, DEFAULT_TARGET_PORT, DEFAULT_LOCAL_PORT
from homeassistant.const import Platform
PLATFORMS = [Platform.LIGHT, Platform.SWITCH]

_LOGGER = logging.getLogger(__name__)


def _cleanup_udp_server(hass):
    """安全关闭旧 UDP 服务器资源"""
    domain_data = hass.data.get(DOMAIN, {})
    if "udp_sock" in domain_data:
        try:
            domain_data["udp_sock"].close()
        except Exception:
            pass
        del domain_data["udp_sock"]
    if "udp_server" in domain_data:
        try:
            transport, protocol = domain_data["udp_server"]
            transport.close()
        except Exception:
            pass
        del domain_data["udp_server"]
    if "udp_bind_port" in domain_data:
        del domain_data["udp_bind_port"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """设置koti集成"""
    hass.data.setdefault(DOMAIN, {})
    entry_id = entry.entry_id
    gateway_id = entry.data.get("gateway_id")
    gateway_host = entry.data.get("host")
    hass.data[DOMAIN][entry_id] = {};
    hass.data[DOMAIN][entry_id]["data"] = entry.data
    hass.data[DOMAIN][entry_id]["current_gateway_id"] = gateway_id
    hass.data[DOMAIN][entry_id]["gateway_host"] = gateway_host
    hass.data[DOMAIN][entry_id]["need_send_request_all"] = True
    # 启动心跳监控任务
    hass.data[DOMAIN][entry_id]["last_heartbeat_time"] = datetime.now() - timedelta(seconds=13)
    hass.data[DOMAIN][entry_id]["heartbeat_monitor_task"] = asyncio.create_task(heartbeat_monitor(hass, entry_id))
    # 初始化 entities 字典，用于 UDP 回调查找实体
    if "entities" not in hass.data[DOMAIN]:
        hass.data[DOMAIN]["entities"] = {}
    hass.data[DOMAIN]["entities"][entry_id] = {}
    # 建立 IP 到 entry_id 的映射（用于 UDP 接收时根据来源 IP 查找）
    if "host_to_entry" not in hass.data[DOMAIN]:
        hass.data[DOMAIN]["host_to_entry"] = {}
    if gateway_host:
        hass.data[DOMAIN]["host_to_entry"][gateway_host] = entry_id
    # 建立 gateway_id 到 entry_id 的映射（用于 UDP 接收时查找）
    if "gateway_to_entry" not in hass.data[DOMAIN]:
        hass.data[DOMAIN]["gateway_to_entry"] = {}
    if gateway_id:
        hass.data[DOMAIN]["gateway_to_entry"][gateway_id] = entry_id
    # 获取目标端口（网关监听端口）
    target_port = entry.options.get("target_port", entry.data.get("target_port", DEFAULT_TARGET_PORT))
    hass.data[DOMAIN][entry_id]["target_port"] = target_port
    desired_local_port  = entry.options.get("local_port", entry.data.get("local_port", DEFAULT_LOCAL_PORT))
    # UDP 服务器只启动一次（所有网关共用）
    if "udp_server" not in hass.data[DOMAIN]:
        # 获取配置的 UDP 端口（默认 6899）
        desired_local_port  = entry.options.get("local_port", entry.data.get("local_port", DEFAULT_LOCAL_PORT))
        # 启动 UDP 服务器，并获取实际绑定的端口（自动避开冲突）
        try:
            desired_local_port = int(desired_local_port)
        except (TypeError, ValueError):
            desired_local_port = DEFAULT_LOCAL_PORT
        actual_local_port  = await start_udp_server(hass, desired_local_port )
        if actual_local_port  is None:
            _LOGGER.error("Failed to start UDP server on any port near %s", desired_local_port )
            return False
        # 将实际端口存储到 DOMAIN 下，供实体发送时使用
        hass.data[DOMAIN]["local_port"] = actual_local_port 
    # 转发给各平台（light, switch 等）
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # 注册卸载处理函数
    entry.async_on_unload(entry.add_update_listener(update_listener))

    # 未来可在此处进行平台转发，如 hass.config_entries.async_forward_entry_setup(entry, "sensor")
    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """卸载koti集成"""
    await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    entry_id = entry.entry_id
    entry_data = hass.data[DOMAIN].get(entry_id)
    #取消心跳监控任务
    if entry_data and "heartbeat_monitor_task" in entry_data:
        entry_data["heartbeat_monitor_task"].cancel()
        del entry_data["heartbeat_monitor_task"]
     # 删除该条目的 entities 子字典
    if "entities" in hass.data[DOMAIN]:
        hass.data[DOMAIN]["entities"].pop(entry.entry_id, None)
     # 删除 gateway 映射
    gateway_id = entry.data.get("gateway_id")
    if gateway_id and "gateway_to_entry" in hass.data[DOMAIN]:
        hass.data[DOMAIN]["gateway_to_entry"].pop(gateway_id, None)
        
    hass.data[DOMAIN].pop(entry_id, None)
    
    # 如果没有其他配置条目，关闭 UDP 服务器
    if len([k for k in hass.data[DOMAIN] if k not in ("gateway_to_entry", "entities", "udp_server", "udp_sock", "udp_bind_port", "local_port")]) == 0:
        if "udp_server" in hass.data[DOMAIN]:
            transport, protocol = hass.data[DOMAIN]["udp_server"]
            transport.close()
            sock = hass.data[DOMAIN].get("udp_sock")
            if sock:
                sock.close()
            del hass.data[DOMAIN]["udp_server"]
            del hass.data[DOMAIN]["udp_sock"]
            if "udp_bind_port" in hass.data[DOMAIN]:
                del hass.data[DOMAIN]["udp_bind_port"]
    return True

async def update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """选项更新处理"""
    await hass.config_entries.async_reload(entry.entry_id)
    
    
async def start_udp_server(hass: HomeAssistant, port: int):
    """尝试绑定端口，如果被占用则递增端口号，最多尝试10次。返回实际绑定的端口，失败返回 None。"""
    loop = asyncio.get_event_loop()
    sock = None
    try:
        # 创建发送 socket 并绑定同一端口
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(('0.0.0.0', port))
        sock.setblocking(False)
        # 使用 asyncio 包装这个 socket
        transport, protocol = await loop.create_datagram_endpoint(
            lambda: UDPStatusProtocol(hass),
            sock=sock
        )
        # 成功，保存
        hass.data.setdefault(DOMAIN, {})
        # 清理旧的可能残留（确保）
        _cleanup_udp_server(hass)
        hass.data[DOMAIN]["udp_server"] = (transport, protocol)
        hass.data[DOMAIN]["udp_sock"] = sock
        hass.data[DOMAIN]["udp_bind_port"] = port
        _LOGGER.info("UDP server bound to port %s", port)
        return port
    except OSError as e:
        # 失败时立即关闭已打开的资源
        _LOGGER.error("OSError on port %s: errno=%s, message=%s", port, e.errno, e.strerror)
        if transport:
            transport.close()
        if sock:
            sock.close()
        if e.errno in (98, 10048):  # Address already in use
            _LOGGER.warning("Port %s already in use, trying next", port)
            return None
        else:
            _LOGGER.error("Unexpected OSError binding UDP port: %s", e)
            return None
    except Exception as e:
        if transport:
            transport.close()
        if sock:
            sock.close()
        _LOGGER.error("Unexpected exception during UDP bind: %s", e)
        return None
    return None

class UDPStatusProtocol(asyncio.DatagramProtocol):
    def __init__(self, hass):
        self.hass = hass
        self.transport = None

    def _get_entry_data_by_gateway_id(self, gateway_id):
        """根据网关 ID 获取对应的配置条目数据"""
        gateway_to_entry = self.hass.data[DOMAIN].get("gateway_to_entry", {})
        entry_id = gateway_to_entry.get(gateway_id)
        if entry_id:
            return self.hass.data[DOMAIN].get(entry_id)
        return None

    def _get_entry_data_by_host(self, host):
        """根据来源 IP 获取对应的配置条目数据"""
        host_to_entry = self.hass.data[DOMAIN].get("host_to_entry", {})
        entry_id = host_to_entry.get(host)
        if entry_id:
            return self.hass.data[DOMAIN].get(entry_id)
        return None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        """同步处理收到的UDP数据包"""
        try:
            xml_str = data.decode("utf-8")
            # 处理状态更新包
            if "<INSTP>" not in xml_str:
                return
            if "<INSTP>DEVICESTATEUPDATEREQ</INSTP>" in xml_str:
                source_host = addr[0]   # UDP 来源 IP
                entry_data = self._get_entry_data_by_host(source_host)
                if not entry_data:
                    _LOGGER.error("No entry data found for host %s", source_host)
                    return
                root = ET.fromstring(xml_str)
                value = root.find(".//VALUE").text
                serial_num = root.find(".//SERIALNUM").text if root.find(".//SERIALNUM") is not None else ""
                
                # 遍历所有 BODY 元素
                for body in root.findall(".//BODY"):
                    device_id = body.find(".//DEVICEID").text
                    value = body.find(".//VALUE").text
                    # 更新对应实体状态
                    for entry_id, entities_dict in self.hass.data[DOMAIN].get("entities", {}).items():
                        entity = entities_dict.get(device_id)
                        if entity:
                            new_state = (value == "00FF")
                            if entity._attr_is_on != new_state:
                                entity._attr_is_on = new_state
                                entity.async_write_ha_state()
                                _LOGGER.info("Updated %s state to %s", device_id, new_state)
                            break  # 找到实体后跳出内层循环
                
                
                # 发送确认包 DEVICESTATEUPDATEACK
                gateway_id = entry_data.get("current_gateway_id")   # 从独立数据中获取
                if not gateway_id:
                    _LOGGER.error("No gateway_id found in current configuration at packet DEVICESTATEUPDATEREQ")
                    return
                # 获取发送socket
                udp_sock = self.hass.data[DOMAIN].get("udp_sock")
                if udp_sock is None:
                    _LOGGER.error("UDP send socket not available at packet HEARTBEAT")
                    return
                target_host = addr[0]  # 来源 IP
                target_port_row = entry_data.get("target_port", DEFAULT_TARGET_PORT)
                target_port = int(target_port_row)
                timestamp = datetime.now().strftime("%Y/%m/%d %H:%M:%S")
                ack_xml = f"""<?xml version="1.0" encoding="utf-8"?>
                                <PACKET>
                                    <HEAD>
                                        <TIMESTAMP>{timestamp}</TIMESTAMP>
                                        <SERVICEID>0001</SERVICEID>
                                        <VERSION>V01.00</VERSION>
                                        <ENCRYTION>0001</ENCRYTION>
                                        <ID>{gateway_id}</ID>
                                        <SERIALNUM>{serial_num}</SERIALNUM>
                                        <LOGINNAME>admin</LOGINNAME>
                                        <PASSWORD>admin</PASSWORD>
                                    </HEAD>
                                    <BODY>
                                        <INSTP>DEVICESTATEUPDATEACK</INSTP>
                                        <RESULT>0</RESULT>
                                    </BODY>
                                </PACKET>"""        
                udp_sock.sendto(ack_xml.encode("utf-8"), (target_host, int(target_port)))
            # 处理心跳包
            elif "<INSTP>HEARTBEAT</INSTP>" in xml_str:
                source_host = addr[0]   # UDP 来源 IP
                entry_data = self._get_entry_data_by_host(source_host)
                if not entry_data:
                    _LOGGER.error("No entry data found for host %s", source_host)
                    return
                entry_data["last_heartbeat_time"] = datetime.now()
                root = ET.fromstring(xml_str)
                serial_num = root.find(".//SERIALNUM").text if root.find(".//SERIALNUM") is not None else ""
                # 直接从当前配置中获取 gateway_id（取第一个非特殊键的条目）
                gateway_id = entry_data.get("current_gateway_id")
                if not gateway_id:
                    _LOGGER.error("No gateway_id found in current configuration at packet HEARTBEAT")
                    return
                # 获取发送socket
                udp_sock = self.hass.data[DOMAIN].get("udp_sock")
                if udp_sock is None:
                    _LOGGER.error("UDP send socket not available at packet HEARTBEAT")
                    return
                target_host = addr[0]
                target_port_row = entry_data.get("target_port", DEFAULT_TARGET_PORT)
                target_port = int(target_port_row)
                timestamp = datetime.now().strftime("%Y/%m/%d %H:%M:%S")
                reply_xml = f"""<?xml version="1.0" encoding="utf-8"?>
                                    <PACKET>
                                    <HEAD>
                                        <TIMESTAMP>{timestamp}</TIMESTAMP>
                                        <SERVICEID>0001</SERVICEID>
                                        <VERSION>V01.00</VERSION>
                                        <ENCRYTION>0001</ENCRYTION>
                                        <ID>{gateway_id}</ID>
                                        <SERIALNUM>{serial_num}</SERIALNUM>
                                        <LOGINNAME>admin</LOGINNAME>
                                        <PASSWORD>admin</PASSWORD>
                                    </HEAD>
                                    <BODY>
                                        <INSTP>HEARTBEATACK</INSTP>
                                    </BODY>
                                    </PACKET>"""
                udp_sock.sendto(reply_xml.encode("utf-8"), (target_host, int(target_port)))
                _LOGGER.info("Sent HEARTBEATACK to %s:%s with gateway_id %s", target_host, target_port, gateway_id)
                if entry_data.get("need_send_request_all"):
                    entry_data["need_send_request_all"] = False
                    self._send_request_all(gateway_id, target_host, int(target_port))
        except Exception as e:
            _LOGGER.error("Error processing UDP packet: %s", e)
       
    def _send_request_all(self, gateway_id: str, target_host: str, target_port: int):
        """发送所有去重后的 control_channel 的请求包"""
        gateway_to_entry = self.hass.data[DOMAIN].get("gateway_to_entry", {})
        entry_id = gateway_to_entry.get(gateway_id)
        if not entry_id:
            _LOGGER.error("No entry found for gateway_id %s", gateway_id)
            return
        entry_data = self.hass.data[DOMAIN].get(entry_id)
        if not entry_data:
            _LOGGER.error("No entry data found for gateway_id %s", gateway_id)
            return

        devices = entry_data.get("data", {}).get("devices", [])
        channels = set()
        for dev in devices:
            ch = dev.get("control_channel")
            if ch:
                channels.add(ch)
        if not channels:
            _LOGGER.warning("No control channels found to request")
            return

        udp_sock = self.hass.data[DOMAIN].get("udp_sock")
        if not udp_sock:
            _LOGGER.error("UDP send socket not available for channel requests")
            return

        timestamp = datetime.now().strftime("%Y/%m/%d %H:%M:%S")
        for ch in channels:
            channel_req_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
                                    <PACKET>
                                        <HEAD>
                                            <TIMESTAMP>{timestamp}</TIMESTAMP>
                                            <SERVICEID>0001</SERVICEID>
                                            <VERSION>01.00</VERSION>
                                            <ENCRYTION>0001</ENCRYTION>
                                            <ID>{gateway_id}</ID>
                                            <SERIALNUM>80000002</SERIALNUM>
                                        </HEAD>
                                        <BODY>
                                            <INSTP>DEVICECONTROLREQ</INSTP>
                                            <CONTROLTYPE>3</CONTROLTYPE>
                                            <DEVICEID>FFFFFFFF</DEVICEID>
                                            <VALUE>0000</VALUE>
                                            <RFID>{ch}</RFID>
                                        </BODY>
                                    </PACKET>"""
            # 直接使用 udp_sock 发送（同步方法，但在异步方法中会阻塞，可以放入 executor）
            # 由于发送很快，可以直接调用
            udp_sock.sendto(channel_req_xml.encode("utf-8"), (target_host, target_port))
            _LOGGER.info("Sent channel request for RFID %s", ch)            

       
async def heartbeat_monitor(hass: HomeAssistant, entry_id: str):
    """监控心跳超时，每1秒检查一次，超时12秒则发送注册包"""
    _LOGGER.info("start  heartbeat_monitor")
    domain_data = hass.data.get(DOMAIN, {})
    # 获取该网关的独立状态字典
    entry_data = domain_data.get(entry_id)
    if not entry_data:
        _LOGGER.error("not entry_data in heartbeat_monitor")
        return
    while True:
        try:
            await asyncio.sleep(1)
            # 如果任务已被取消或集成已卸载，退出
            if not entry_data.get("heartbeat_monitor_task"):
                break
            last_time = entry_data.get("last_heartbeat_time")
            if not last_time:
                # 初始时未设置，则设置为当前时间，并跳过本次检测（或直接继续）
                entry_data["last_heartbeat_time"] = datetime.now()
                continue
            elapsed = (datetime.now() - last_time).total_seconds()
            if elapsed > 12:
                #_LOGGER.warning("Heartbeat timeout detected")
                gateway_id = entry_data.get("current_gateway_id")
                udp_sock = domain_data.get("udp_sock")
                target_port = entry_data.get("target_port", DEFAULT_TARGET_PORT)
                entry_data["need_send_request_all"] = True   # 改为使用 entry_data
                # 获取网关 IP
                host = entry_data.get("gateway_host")   # 直接获取
                _LOGGER.warning("Heartbeat Check: gateway_id=%s, udp_sock=%s, host=%s, target_port=%s", gateway_id, udp_sock, host, target_port)            
                if gateway_id and udp_sock and host:
                    timestamp = datetime.now().strftime("%Y/%m/%d %H:%M:%S")
                    register_xml = f"""<?xml version="1.0" encoding="utf-8"?>
                                        <PACKET>
                                            <HEAD>
                                                <TIMESTAMP>{timestamp}</TIMESTAMP>
                                                <SERVICEID>0001</SERVICEID>
                                                <VERSION>V01.00</VERSION>
                                                <ENCRYTION>0001</ENCRYTION>
                                                <ID>{gateway_id}</ID>
                                                <SERIALNUM>80000001</SERIALNUM>
                                                <LOGINNAME>admin</LOGINNAME>
                                                <PASSWORD>admin</PASSWORD>
                                            </HEAD>
                                            <BODY>
                                                <INSTP>REGISTERMAGICTOUCHREQ</INSTP>
                                                <NAME>koticonfig</NAME>
                                            </BODY>
                                        </PACKET>"""
                    try:
                        loop = asyncio.get_event_loop()
                        await loop.run_in_executor(None, udp_sock.sendto, register_xml.encode("utf-8"), (host, int(target_port)))
                        _LOGGER.warning("Heartbeat timeout, sent REGISTERMAGICTOUCHREQ to %s:%s", host, target_port)
                    except Exception as send_err:
                        _LOGGER.error("Failed to send REGISTERMAGICTOUCHREQ: %s", send_err, exc_info=True)    
                        # 重置心跳时间，避免连续发送
                    entry_data["last_heartbeat_time"] = datetime.now()     
                    
        except asyncio.CancelledError:
            _LOGGER.debug("Heartbeat monitor task cancelled")
            break
        except Exception as e:
            _LOGGER.error("Unexpected error in heartbeat_monitor: %s", e, exc_info=True)
            await asyncio.sleep(1)  # 避免快速循环        
            
