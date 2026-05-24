# Koti(柯帝)智能家居接入HomeAssistant

![logo](custom_components/koti/brand/logo.png)

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg?style=for-the-badge)](https://github.com/hacs/integration)

Koti Homeassistant 集成是一款专为 Koti(柯帝)智能家居 **全能家电控制器 SGW1000** 打造的自定义组件。它支持通过本地网络直接与网关进行通信，获取网关下的所有子设备，无需依赖官方云服务，从而实现更快速、更可靠的设备状态更新和控制。

该集成支持通过 SSDP 自动发现网关，同时也兼容手动添加方式，可以一次性获取网关下全屋子设备的列表，并自动在 Home Assistant 中创建对应的实体实例。

该集成目前处于**积极开发中**，欢迎使用和反馈。

## 🏠 适用设备与固件

- **全能家电控制器 SGW1000**：需要提前接入家庭局域网，并知晓其 IP 地址。
- **SGW1000 配套子设备**：
  - 智能灯、开关灯、插座等。
  - **注意**：目前仅支持 `type=1`（灯具）和 `type=3`（插座/开关）的设备，不支持电池供电类传感器设备。

> ⚠️ **网络限制**：
> 网关的 SSDP 多播发现可能受限于复杂的网络环境。若自动发现失败，请优先选择“手动配置”模式，输入正确的网关 IP 地址。
> 使用集成添加网关后，网关的 IP 地址将和条目关联，因此需要在局域网中给网关一个固定 IP 地址
## 📦 安装方式

### 通过 HACS 安装（推荐）

1. 确保已安装 [HACS](https://hacs.xyz/)。
2. 在 HACS 中添加自定义存储库：
   - 存储库地址：`https://github.com/zhutingxf/KotiHomeassistant`
   - 类别：**集成**
3. 在 HACS 中找到并安装 "Koti Homeassistant"。
4. 完全重启 Home Assistant。

### 手动安装

1. 从 [Github 发布页](https://github.com/zhutingxf/KotiHomeassistant) 下载最新的发布包。
2. 将 `custom_components/koti` 文件夹复制到你的 Home Assistant `custom_components` 目录中。
3. 完全重启 Home Assistant。

## ⚙️ 配置方式

安装完成后，你可以通过 Home Assistant 的 UI 界面完成设备配置：

**设置 → 设备与服务 → 添加集成 → 搜索 "Koti Homeassistant"**

### 自动发现网关

1. 在集成向导中选择 **自动发现网关**。
2. Home Assistant 会自动扫描局域网内的 SGW1000 网关。
3. 从发现的设备列表中选择正确的网关，点击提交。
4. 集成将自动获取网关下的设备列表，并为每个设备创建实体（灯或插座）。

> 💡 **提示**：如果你遇到“未发现任何 koti 网关”的提示，请检查网关是否正确接入局域网，或尝试使用**手动配置**方式。

### 手动指定网关

1. 在集成向导中选择 **手动指定网关**。
2. 输入你的 SGW1000 网关的 IP 地址（例如 `10.5.3.1`）。
3. 点击提交，集成将通过 FTP 获取网关配置信息并创建设备实体。

### 支持的实体类型

- **智能灯 (type=1)**：在 Home Assistant 中显示为 `light` 实体，支持开/关操作。
- **智能插座/开关 (type=3)**：在 Home Assistant 中显示为 `switch` 实体，支持开/关操作。

## 🐛 问题反馈与贡献

- 该集成的开发基于抓包与逆向工程，不同批次或固件版本的 SGW1000 网关可能存在差异。
- 如果你遇到设备无法识别或控制的问题，请访问 [Github Issues 页面](https://github.com/zhutingxf/KotiHomeassistant/issues) 提交反馈。
- 提交反馈时，请附上 Home Assistant 日志中与 `koti` 相关的 DEBUG 级别日志。

## 💡 致谢

- 感谢所有参与测试和反馈的用户。
- 受 [tuya-local](https://github.com/make-all/tuya-local) 集成启发。

---

**Koti HomeAssistant** 由 [zhutingxf](https://github.com/zhutingxf) 维护。