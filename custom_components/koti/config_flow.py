import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback, HomeAssistant
from .const import DOMAIN, CONF_HOST, DEFAULT_TARGET_PORT, DEFAULT_LOCAL_PORT
import re  # 用于正则验证IP
import ftplib
import sqlite3
import io
from homeassistant.components import ssdp
import logging
import tempfile
import os
import asyncio
import socket
from email.parser import Parser
from urllib.parse import urlparse


_LOGGER = logging.getLogger(__name__)
# 手动配置的表单（只要求网关 IP）
MANUAL_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST, default="192.168.1."): str,
    }
)

class KotiConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        """用户点击添加集成时，直接进入选择页面"""
        return await self.async_step_choose_method()

    async def async_step_choose_method(self, user_input=None):
        """选择配置方式：自动发现 or 手动指定"""
        errors = {}
        if user_input is not None:
            if user_input["configuration_gateway_method"] == "auto":
                return await self.async_step_auto_discover()
            else:
                return await self.async_step_manual()

        return self.async_show_form(
            step_id="choose_method",
            data_schema=vol.Schema({
                vol.Required("configuration_gateway_method", default="auto"): vol.In(["auto", "manual"])
            }),
            errors=errors or {},
            last_step=False,
        )

    async def async_step_manual(self, user_input=None):
        """手动指定网关地址（只要求 IP）"""
        errors = {}
        if user_input is not None:
            host = user_input[CONF_HOST]
            # 验证IP格式
            if not self._is_valid_ip(host):
                errors[CONF_HOST] = "invalid_ip_format"  # 错误key，对应翻译文件
            else:
                # IP格式正确，保存并跳转到下一步（例如确认页面）
                self._host = host
                return await self.async_step_final()  # 跳转到最终确认步骤
        return self.async_show_form(
            step_id="manual",
            data_schema=MANUAL_SCHEMA,
            errors=errors,
            last_step=False,
        )

    async def async_step_auto_discover(self, user_input=None):
        """主动发送 SSDP 搜索，并展示发现的设备列表供用户选择"""
        if user_input is not None:
            # 用户取消了进度（点击“取消”按钮）
            return self.async_abort(reason="user_cancel")
        # 进度条任务改为调用一个“占位”协程，实际发现放在 progress 步骤中
        async def _placeholder():
            pass
        # 1. 创建一个用于执行 SSDP 搜索的后台任务
        # 注意：这里只是创建任务，还没有执行，需要传递给 async_show_progress 来执行
        discovery_task = self.hass.async_create_task(_placeholder())
        
        # 2. 显示进度条
        return self.async_show_progress(
            step_id="auto_discover_progress",
            progress_action="discover_koti_gateway",
            progress_task=discovery_task,
            description_placeholders={},  # 确保翻译中的描述被正确加载
        ) 

    async def async_step_auto_discover_progress(self, user_input=None):
        """进度条完成后自动调用，处理后续"""
        if user_input is not None:
            return self.async_abort(reason="user_cancel")
        # 直接执行发现（耗时操作），完成后关闭进度条
        self._discovered_devices = await self._send_ssdp_search_and_listen(timeout=5)
        # 此时 _do_discovery 已经完成，_discovered_devices 已保存
        return self.async_show_progress_done(next_step_id="auto_discover_done")
    

    #async def _do_discovery(self):
    #    try:
    #        self._discovered_devices = await self._send_ssdp_search_and_listen(timeout=10)
    #        _LOGGER.error("_do_discovery completed, found %d devices", len(self._discovered_devices))
    #    except Exception as e:
    #        _LOGGER.error("_do_discovery error: %s", e, exc_info=True)
    #        self._discovered_devices = []
    #    return self._discovered_devices
    

    async def async_step_auto_discover_done(self, user_input=None):
        """显示搜索结果：设备列表或未发现提示"""
        if user_input is not None:
            # 用户从列表中选择了一个设备
            idx = user_input.get("device_index")
            if idx is not None and self._discovered_devices:
                selected = self._discovered_devices[int(idx)]
                self._host = selected["host"]   # 设置网关 IP
                # 可选：保存 USN / gateway_id 等
                return await self.async_step_final()   # 跳转到获取网关信息步骤
            return self.async_abort(reason="no_device_selected")

        if not self._discovered_devices:
            # 未发现任何设备
            return self.async_abort(reason="no_devices_found")
        
        # 发现设备，构建设备选择表单
        options = {}
        for i, dev in enumerate(self._discovered_devices):
            label = f"IP: {dev['host']}"
            if dev.get('gateway_id'):
                label += f"  -  ID: {dev['gateway_id']}"
            options[str(i)] = label

        data_schema = vol.Schema({
            vol.Required("device_index"): vol.In(options)
        })
        return self.async_show_form(
            step_id="auto_discover_done",
            data_schema=data_schema,
            description_placeholders={"count": len(self._discovered_devices)},
            last_step=False,
        )

    async def async_step_final(self, user_input=None):
        """最终步骤：尝试获取网关信息并存储"""
        if user_input is not None:
             # 手动检查当前所有配置条目
             # 用户点击了“提交”，此时 self._gateway_data 应该已经在首次进入时保存
            if not hasattr(self, '_gateway_data') or not self._gateway_data:
                return self.async_abort(reason="unknown")

            gateway_id = self._gateway_data["gateway_id"]
            existing_entries = self._async_current_entries()
            for entry in existing_entries:
                if entry.data.get("gateway_id") == gateway_id:
                    return self.async_abort(reason="already_configured")
    # 未发现重复，继续创建
            # 用户点击“提交”，此时可以使用 self._gateway_data 中的数据创建配置条目
            # 例如：将设备列表存入配置条目的 data 中
            # 暂时先只创建条目，后续可扩展
            return self.async_create_entry(
                title = f"{self._gateway_data['name']}  -  {self._gateway_data['gateway_id']}",
                data={
                    "host": self._host,
                    "gateway_id": self._gateway_data["gateway_id"],
                    "name": self._gateway_data["name"],
                    "target_port": self._gateway_data["udp_recv_port"],
                    "local_port": self._gateway_data["udp_send_port"],  
                    "devices": self._gateway_data["devices"],
                },
            )

        # 首次进入：执行 FTP 下载和数据库查询
        try:
            gateway_data = await self._fetch_gateway_info()
            self._gateway_data = gateway_data  # 保存以供后续使用
            gateway_id = gateway_data["gateway_id"]
            gateway_name = gateway_data["name"]
            udp_port = gateway_data["udp_recv_port"] or "未配置"
            device_count = len(gateway_data["devices"])

            # 显示确认页面，可显示更多信息
            return self.async_show_form(
                step_id="final",
                description_placeholders={
                    "gateway_id": gateway_id,
                    "name": gateway_name,
                    "udp_recv_port": udp_port,
                    "device_count": str(device_count),
                },
                last_step=True,
            )
        except Exception as e:
            _LOGGER.error("Failed to fetch gateway info: %s", e)
            # 回到手动输入步骤，并显示错误
            manual_schema = vol.Schema({vol.Required(CONF_HOST, default=self._host): str})
            return self.async_show_form(
                step_id="manual",
                data_schema=manual_schema,
                errors={"base": "fetch_failed"},
                last_step=False,
            )

    async def _fetch_gateway_info(self):
        """在 executor 中执行同步的 FTP + SQLite 操作"""
        return await self.hass.async_add_executor_job(self._fetch_gateway_info_sync)
        
    def _fetch_gateway_info_sync(self):
        """同步方法：通过 FTP 下载文件并读取数据库（使用内存数据库 deserialize）"""
        host = self._host
        try:
            # 连接 FTP（匿名登录，超时10秒）
            ftp = ftplib.FTP(host, timeout=10)
            ftp.login()
            # 下载文件到内存字节流
            file_data = io.BytesIO()
            ftp.retrbinary("RETR koti.kotix", file_data.write)
            ftp.quit()
            db_bytes = file_data.getvalue()
            
            # 直接反序列化为内存数据库
            conn = sqlite3.connect(":memory:")
            conn.deserialize(db_bytes)  # 需要 Python 3.11+
            cursor = conn.cursor()
            cursor.execute("SELECT gateway_id, name FROM sys LIMIT 1")
            row = cursor.fetchone()  
            if row is None:
                raise Exception("表 sys 为空或无记录")
            gateway_id, gateway_name = row

            # 2. 从 configure 表获取 udp_recv_port
            cursor.execute("SELECT param_value FROM configure WHERE param_name = 'udp_recv_port'")
            port_row = cursor.fetchone()
            udp_recv_port = port_row[0] if port_row else None
            
            # 在查询 udp_recv_port 之后添加
            cursor.execute("SELECT param_value FROM configure WHERE param_name = 'udp_send_port'")
            send_port_row = cursor.fetchone()
            udp_send_port = send_port_row[0] if send_port_row else 16878   # 默认值改为 16878
            
            # 3. 从 device 表获取所有设备信息
            cursor.execute("SELECT device_id, control_channel, type, name FROM device")
            devices = cursor.fetchall()
            devices_list = []
            for dev in devices:
                devices_list.append({
                    "device_id": dev[0],
                    "control_channel": dev[1],
                    "type": dev[2],
                    "name": dev[3],
                })

            conn.close()
            return {
                "gateway_id": gateway_id,
                "name": gateway_name,
                "udp_recv_port": udp_recv_port,
                "udp_send_port": udp_send_port,   # 新增
                "devices": devices_list,
            }
        except ftplib.error_perm as e:
            raise Exception("无法下载 koti.kotix 文件，请确认文件存在且 FTP 可访问") from e
        except sqlite3.Error as e:
            raise Exception("下载的文件不是有效的 SQLite 数据库或缺少 sys 表") from e
        except Exception as e:
            raise Exception(f"连接网关失败: {e}") from e
    

    async def _send_ssdp_search_and_listen(self, timeout):
        """主动发送 M-SEARCH，并监听响应（先监听再发送）"""
        _LOGGER.info("=== SSDP: Starting search, timeout=%s ===", timeout)
        multicast_addr = "255.255.255.255"
        multicast_port = 1900
        st = "urn:schemas-UPnP-org:device:SmartHomeCenterDevice:1"

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.setblocking(False)
        loop = asyncio.get_event_loop()
        _LOGGER.info("SSDP: Socket bound to port %s", sock.getsockname()[1])
        # 创建一个队列存放响应
        responses = []
        # 定义一个异步接收函数
        async def receive():
            while True:
                try:
                    data, addr = await loop.sock_recvfrom(sock, 4096)
                    _LOGGER.info("SSDP: Received %d bytes from %s", len(data), addr)
                    device = self._parse_ssdp_response(data.decode(errors='ignore'), addr[0])
                    if device and device not in responses:
                        responses.append(device)
                        _LOGGER.info("SSDP: Parsed device: %s", device)
                    else:
                        _LOGGER.error("SSDP: Failed to parse response from %s", addr)    
                except asyncio.CancelledError:
                    # 任务被取消，正常退出
                    _LOGGER.info("SSDP: Receive task cancelled")
                    break
                except Exception as e:
                    _LOGGER.error("SSDP: Receive error: %s", e)
                    break

        # 启动接收任务
        recv_task = asyncio.create_task(receive())
        #_LOGGER.error("SSDP: Receive task started")
        # 发送 M-SEARCH
        request = (
            "M-SEARCH * HTTP/1.1\r\n"
            f"HOST: {multicast_addr}:{multicast_port}\r\n"
            'MAN: "ssdp:discover"\r\n'
            "MX: 3\r\n"
            f"ST: {st}\r\n"
            "\r\n"
        )
        _LOGGER.info("SSDP: Sending M-SEARCH request to %s:%s", multicast_addr, multicast_port)
        await loop.sock_sendto(sock, request.encode(), (multicast_addr, multicast_port))
        _LOGGER.info("SSDP: M-SEARCH sent, waiting for responses...")
        # 等待超时后取消接收任务
        await asyncio.sleep(timeout)
        recv_task.cancel()
        sock.close()
        _LOGGER.info("SSDP: Search finished, found %d devices", len(responses))
        return responses

    def _parse_ssdp_response(self, response: str, source_ip: str):
            _LOGGER.info("SSDP: Parsing response from %s", source_ip)
            lines = response.split('\r\n')
            if not lines:
                _LOGGER.error("SSDP: Empty response")
                return None
            if not lines[0].startswith('HTTP/1.1 200'):
                _LOGGER.error("SSDP: Not a 200 OK, first line: %s", lines[0])
                return None
            header_text = '\r\n'.join(lines[1:])
            msg = Parser().parsestr(header_text)
            headers = {k.lower(): v for k, v in msg.items()}
            # 匹配 ST：只要包含设备类型关键字即可（兼容缺 urn: 的情况）
            st_value = headers.get('st', '')
            if 'SmartHomeCenterDevice:1' not in st_value:
                _LOGGER.error("SSDP: ST mismatch, got: %s", st_value)
                return None
            location = headers.get('location')
            if not location:
                _LOGGER.error("SSDP: No location header")
                return None

            try:
                parsed = urlparse(location)
                host = source_ip
            except Exception:
                host = source_ip
            usn = headers.get('usn', '')
            # USN 格式: "100111113B00265:: urn:schemas-UPnP-org:device:SmartHomeCenterDevice:1"
            gateway_id = usn.split('::')[0] if usn else None
            _LOGGER.info("SSDP: Successfully parsed device: host=%s, gateway_id=%s", source_ip, gateway_id)
            return {
                "host": host,
                "location": location,
                "usn": usn,
                "gateway_id": gateway_id,
            }



    async def async_step_ssdp(self, discovery_info: ssdp.SsdpServiceInfo) -> FlowResult:
        """处理由 HA SSDP 扫描器发现的设备"""
        _LOGGER.error("SSDP discovery: %s", discovery_info)

        # 提取设备信息
        location = discovery_info.ssdp_location  # 例如 http://10.5.3.1:8080/device.xml
        if not location:
            return self.async_abort(reason="no_location")

        # 从 location 解析 IP 和端口
        parsed = urlparse.urlparse(location)
        host = parsed.hostname
        port = parsed.port or 8080

        # 可选：检查 ST 是否匹配（实际上 ssdp 已经过滤了）
        st = discovery_info.ssdp_st
        if st != "urn:schemas-upnp-org:device:SmartHomeCenterDevice:1":
            return self.async_abort(reason="not_supported")

        # 去重：检查是否已配置相同 IP 或相同网关 ID
        for entry in self._async_current_entries():
            if entry.data.get("host") == host:
                return self.async_abort(reason="already_configured")
            # 如果可以从其他字段（如 USN）提取唯一 ID，可以进一步判断

        # 保存发现的信息
        self._discovered_host = host
        self._discovered_port = port

        # 进入用户确认步骤
        return await self.async_step_discovery_confirm()

    async def async_step_discovery_confirm(self, user_input=None):
        """用户确认发现设备后，自动获取网关信息并完成配置"""
        if user_input is not None:
            self._host = self._discovered_host
            # 跳转到最终获取网关信息的步骤（复用已有的 FTP 逻辑）
            return await self.async_step_final()

        return self.async_show_form(
            step_id="discovery_confirm",
            description_placeholders={
                "host": self._discovered_host,
            },
        )

    # 辅助方法：验证IP地址格式（IPv4）
    @staticmethod
    def _is_valid_ip(ip: str) -> bool:
        pattern = re.compile(r'^((25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}(25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)$')
        return bool(pattern.match(ip))
        
    # 在 KotiConfigFlow 类内部，确保有这个方法（取消注释）：
    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """返回选项流处理程序"""
        return KotiOptionsFlow()    
        
# 在 KotiConfigFlow 类外部（同一文件末尾），添加 OptionsFlow 类：
class KotiOptionsFlow(config_entries.OptionsFlow):
    """处理集成的选项（IP和端口修改）"""

    async def async_step_init(self, user_input=None):
        """显示选项表单，让用户修改 IP 和端口"""
        if user_input is not None:
            # 用户提交了新配置，保存到 options 中
            return self.async_create_entry(title="", data=user_input)

        # 当前值（如果没有保存过选项，则从 data 中读取，否则从 options 中读取）
        current_host = self.config_entry.options.get("host", self.config_entry.data.get("host", ""))
        current_target = self.config_entry.options.get("target_port", self.config_entry.data.get("target_port", DEFAULT_TARGET_PORT))
        current_local = self.config_entry.options.get("local_port", self.config_entry.data.get("local_port", DEFAULT_LOCAL_PORT))

        # 定义表单字段
        schema = vol.Schema({
            vol.Required("host", default=current_host): str,
            vol.Required("target_port", default=current_target): int,
            vol.Required("local_port", default=current_local): int,
        })
        return self.async_show_form(step_id="init", data_schema=schema)        