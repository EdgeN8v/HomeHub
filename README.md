# HomeHub：个人版"米家"

一个入口控制家里所有品牌的设备，没有广告；把每天重复的操作（比如晚上约洗衣机）变成自动化；以后接上自己的语音助手和自研设备。

## 分层

```
我们自己写：手机 App / 语音助手（大模型）/ 自动化逻辑   ← 只认"洗衣机""客厅空调"
                 │  Home Assistant 的 API
Home Assistant（设备层，跑在香橙派上）                  ← 当驱动用，平时不用打开它的界面
   ├─ 小米（官方 xiaomi_home 集成）
   ├─ 以后换的其他品牌 / Matter
   └─ 自研 ESP32 设备（ESPHome）
```

换品牌时只在 HA 里换设备，上面一层不用改。

## 机器分工

| 机器 | 地址 | 用途 |
|---|---|---|
| 香橙派 Orange Pi 5（RK3588S 8 核，8G，NVMe 119G，Ubuntu 24.04） | `192.168.31.61`，用户 `CUI`，密钥 `~/.ssh/id_ed25519` | Home Assistant；以后的语音识别 / 语音合成。同机还有 Immich 的 `immich_ml` |
| NAS 极空间 Z2Pro（ARM64，3.6G） | `192.168.31.131`，SSH 端口 10000 | 只做存储和已有服务。内存本来就紧（在用交换分区），不放 HomeHub 的东西 |

- 香橙派的 `CUI` 在 docker 组里，不需要 sudo。电脑公钥是用 `tools\add_ssh_key.ps1` 加上去的（运行时要输一次密码，之后都免密）。
- 建议在小米路由器里给香橙派绑定固定 IP。

## 部署

| 项目 | 值 |
|---|---|
| HA 网页 | http://192.168.31.61:8123（只在局域网） |
| 香橙派上的目录 | `/home/CUI/homehub/homeassistant/config` |
| 镜像 | `ghcr.nju.edu.cn/home-assistant/home-assistant:stable`（南大镜像，直连 ghcr.io 慢） |
| 当前版本 | HA 2026.10.2，xiaomi_home v0.5.0 |

- 部署或更新：`.\tools\deploy_ha.ps1`，直接 `docker run`，`--network host`（米家局域网发现和直连需要）。
- 小米集成：`xiaomi_home`（[XiaoMi/ha_xiaomi_home](https://github.com/XiaoMi/ha_xiaomi_home)）手动放在 `config/custom_components/xiaomi_home/`，在电脑上从 GitHub 下载后拷过去。
- `.ps1` 脚本里有中文注释的，要存成 **UTF-8 带 BOM**，否则 Windows PowerShell 5.1 会把它读乱、报语法错。
- 2026-10-22：HA 一开始部署在 NAS 上，因为 NAS 内存不够，当天迁到了香橙派。

- 小米集成登录的坑：授权后跳到 `http://homeassistant.local:8123/...` 打不开（香橙派主机名不是 homeassistant），把地址里的 `homeassistant.local` 换成 `192.168.31.61` 再打开即可。
- HA 访问令牌：在 HA 个人资料 → 安全 → 长期访问令牌创建（名称 `claude`），用 `tools\save_ha_token.ps1` 存进 `.env`（不进 git）。`python tools/ha.py states 洗衣` 可以查实体。

## 后台服务 hub/（我们自己的那一层）

跑在香橙派上，容器 `homehub-hub`，**https://192.168.31.61:8200**（家里）/ **https://你的域名:8095**（公网，域名写在本机 `.env` 的 `PUBLIC_HOST`）。部署：`.\tools\deploy_hub.ps1`。

- Python + aiohttp。通过 HA 的 REST / WebSocket 控制设备，不直接碰米家。令牌在香橙派 `~/homehub/hub/hub.env`（600，每次部署从电脑 `.env` 重新生成），数据在 `~/homehub/hub/data/`（`hub.db`、`tls/`、`updates/`）。
- **HTTPS + 令牌**：自签证书（`hub/certs/`，`tools/gen_secrets.py` 生成），手机 App 固定证书公钥指纹。除图标外所有请求都要带 `HUB_TOKEN`：`Authorization: Bearer` 头，或 `hub_token` cookie。浏览器访问先打开一次 `https://192.168.31.61:8200/login?token=<HUB_TOKEN>`（会有自签证书警告）。
- 公网转发都从 NAS 进来，服务看到的来源 IP 都是 NAS，所以没有按 IP 封禁；令牌是 32 字节随机数，靠长度防猜。
- **全屋页面**（`/`，`static/index.html` + `app.js` + `app.css` + `icons.js`）：米家风格首页（家庭、房间标签、设备卡片和开关按钮、室内温湿度、烟雾/燃气/水浸状态）和设备详情页（按类型：空调温度/模式/风速、灯亮度/色温、风扇、加湿器、热水器、音量；功能列表；更多设置）。洗衣机详情页链到 `/washer`。
  - 设备模型在服务端算好（`app/home.py`，测试 `tests/test_home.py`）：从 HA 的设备/实体/区域注册表读出每个设备，按型号和实体判断类型（kind），挑出卡片开关（toggles）、状态角色（roles：温度、电量、工作状态、剩余时间…）、常用功能（features）和更多设置（more）。小米实体名是"设备  服务 属性"，带 `*` 的非标准属性不显示；提示音、童锁、指示灯、开发者模式之类归到更多设置。区域名"我的家 卧室"拆成家庭 + 房间。
  - `GET /api/home` 返回模型和状态，`GET /api/stream`（SSE）推送状态变化，`POST /api/call {entity_id, service, data}` 控制设备：只允许模型里的实体、白名单里的服务（`home_service.py` 的 `ALLOWED`）。
  - 页面在服务器上，改完 `.\tools\deploy_hub.ps1` 部署，手机 App 重新打开就是新的，不用发 App。
  - 电脑上预览：`python tools/dev_proxy.py` → http://localhost:8300（代理带令牌、校验证书指纹，静态文件直接读本地，改完刷新即可）。
- **洗衣记录**：监听洗衣机相关实体的每次状态变化存进 SQLite；每次（重新）连上 HA 都从 HA 历史补录最近 10 天。一次"洗衣"= 状态进入「工作中」到离开「工作中/暂停中」，记录实际用时和开始时洗衣机的预估时长（`hub/app/washer_logic.py`，测试在 `hub/tests/`）。
- **一键约洗衣机** `POST /api/washer/book {"drums":["upper","lower"],"finish_by":"05:50","dry_run":false}`：
  每个桶检查开机/待机中/门/故障 → 模式切到「自定义」→ 读洗衣机预估时长 → 预约方式设为「预约完成」(Finish Wash)，预约剩余时间 = 到（目标时间 − 余量）的分钟数 → 按「开始洗涤」→ 等它进入「预约中」确认。
  余量 = 最近 10 次「自定义」洗衣里实际比预估多用的最大分钟数 + 5（限制在 5–60），记录不足 3 次时用 20 分钟。
- 洗衣机实体：上桶是 MIoT 服务 12，下桶是 13（`mibx5.x13`，实体前缀 `mibx5_cn_860976874_x13`）。
- **待验证**：远程按「开始洗涤」是否需要先在洗衣机上打开"安全远程控制"（`child_protected_status`，目前是关）。第一次真实预约时确认。
- 谷电 22:00–06:00；默认 05:50 前洗完。

## 断网能力（2026-10-22 实测）

目标：自动化都放在香橙派的 HA 里跑，断网照样执行；米家只用来登录。

- 小米集成：控制模式"自动"，已连上**中枢网关本地通道**（`.storage/xiaomi_home/cert/`），并打开了**局域网控制**（网口 `end1`，订阅开启；集成选项 → 更新局域网控制配置）。
- 实测方法：`tools/offline_test.sh on 15` 在香橙派上加防火墙规则，只拦外网（局域网、docker、组播照通），并起一个 15 分钟后自动撤销的看门狗容器；`off` 立即恢复，`status` 查看。用户 CUI 没有免密 sudo，所以是用特权容器 nsenter 到主机执行的。
- 结果（断网约 8 分钟）：
  - 台灯（Wi-Fi）开关：断网时 0.2–0.3 秒生效；**断网状态下重启 HA** 后同样 0.2–0.3 秒生效。
  - 4 个人体传感器、厕所浴霸、温湿度计：断网期间持续上报。
  - 断网重启 HA：小米集成正常加载，不可用实体数和重启前一样（370，联网时 362）。
  - 断网后掉线的只有本来就只能走云的：标签打印机、充电器、崔家的腰部按摩仪（崔家没有中枢网关）、辽阳的路由器。
  - 天气（met.no）断网时取不到，属正常。
- 没测到的：空调、开关等其他设备只做了被动观察（没去动它们）；小米登录凭证 3 天过期，断网超过 3 天会怎样没测。

## 公网入口（NAS）

NAS 上的容器 `homehub-forward`（nginx stream，`nas/nginx.conf`）：公网 IPv6 **8095** → 香橙派 8200，只转发 TCP，不解密。部署：`.\tools\deploy_nas.ps1`。极空间防火墙已放行 TCP 8095（2026-10-22）。家里只有 IPv6 公网，在外面的手机网络要有 IPv6。

## 手机 App android/

安卓原生"外壳"（Kotlin，无 Compose），包名 `com.homehub.app`，最低 Android 10。里面是 WebView，显示 hub 的网页，所以**网页改动不用发 App**。外壳负责：

- 按证书指纹信任 hub（`Net.kt`；WebView 里在 `onReceivedSslError` 比对指纹），带上令牌 cookie；
- **页面挂在虚拟地址 `https://homehub.local/`**（1.8 起）：它不是真服务器，WebView 对它的所有请求都由 `ShellCache.kt` 接手——页面文件（`/`、`/washer`、`/static/*`）从手机里的副本读（缓存优先，后台刷新，新版页面会先把它引用的 `?v=` 资源下载齐再替换），API 和实时推送经固定证书的 OkHttp 转发到当前线路。所以：断网也能打开（显示 localStorage 里上次的状态并自动重连），在家/在外切换不用重载，WebView 本身从不和 hub 做 TLS。安卓拦截不到 POST 正文，页面会把正文 base64 后再放进 `X-HH-Body` 头（`app.js` 的 `withBody()`），没带这个头的 POST 一律不转发；
- 选路线：家里地址 400ms 内能连上就走家里，否则公网能连上走公网，都连不上（断网）就保持上次的线路；网络恢复时（系统网络回调）立刻让页面重连；
- **一键更新**（`Updater.kt`，和 Music / LearnSync 同一套）：打开时检查 `/api/updates/version.json`，有新版弹窗，点"更新"下载并调起系统安装。网页里通过 `window.HomeHubApp` 显示版本和"检查更新"。

构建需要（都在 .gitignore 里，**丢了就要重装 App**）：
- `android/secrets.properties`：地址、令牌、证书指纹，`python tools/gen_secrets.py` 生成；
- `android/homehub.jks` + `android/keystore.properties`：签名密钥。**丢了以后就不能覆盖更新，只能卸载重装**，记得备份。

发布新版：改 `android/app/build.gradle.kts` 的 `versionCode`（+1）和 `versionName` → 在 `CHANGELOG.md` 顶部写说明 → `.\tools\publish_app.ps1`（编译、上传到香橙派）。第一次安装用 USB：`adb install -r android\app\build\outputs\apk\release\app-release.apk`。

## 自动化（从米家迁过来）

- 自动化都放在 HA 里（香橙派本地，断网照常），App 的「智能」页列出、开关、手动执行；点进去是编辑器（`static/auto.js`），右上角「+」新建。
- 编辑器是米家的样子：如果（任一满足：设备状态 / 按键事件 / 数值高于低于 / 每天定时）、生效时间（时间段 + 星期几）、就执行（设备动作 / 延时，按顺序）。它在 HA 配置和这个视图之间双向转换（`parseAuto` / `buildAuto`），转不了的（在 HA 里手写的复杂规则）只读显示，不会被改坏。保存时自动生成「如果 … → …」描述写进 HA 的 description。
- 后端 `GET/POST /api/automations/<entity_id|new>`、`POST /api/automations/<id>/delete`：只接受编辑器能生成的形状、本家里的实体和白名单服务（`home_service.py` 的 `check_automation`）。删除用 POST，因为 App 的请求中转只转 GET/POST。
- 描述以「草稿｜」开头的显示草稿标签；在编辑器里保存一次就算确认了，标签消失。
- 米家的自动化读不出来（2026-10-22 试过，用户授权、只读）：HA 小米集成的云端授权只开放设备接口，场景/自动化接口全是 404；中枢网关的本地通道只回答 `proxy/getMijiaActionGroupList`（6 个手动场景的 ID，没有名字和内容），自动化相关请求一概超时。另外网关只认 HA 自己的客户端 ID，用同一个 ID 连上去会把 HA 的本地连接挤掉（HA 会自己重连，保险起见可以重载一次小米集成）。
- 所以 6 条是照米家截图里的名字和设备图标写的草稿（automation id 以 `mijia_` 开头），默认关闭：米家里的同名自动化还在跑，两边都开会重复触发。确认一条就打开它，同时在米家里关掉那条。

## 添加新设备

- 小米设备：只能在米家 App 里配网（小米私有流程）。配好后小米集成会把它同步进 HA，HomeHub 的设备模型约 10 分钟刷新一次就会出现，米家里的房间也跟着过来。
- 没分配房间的设备显示在首页的「未分配」分组（排在最前），用长按 → 移动设备放进房间。HA 自己的东西（日出日落、备份、天气、蓝牙适配器等，见 `home.py` 的 `INFRA_DOMAINS`）不会出现。
- 以后买设备优先 Zigbee（香橙派插 Zigbee 接收棒）或 Matter（香橙派上跑 Matter 服务，用它的蓝牙配网）：不用厂商 App、不靠云、断网照常。自研设备用 ESPHome，HA 自动发现。App 里的「+ 添加设备」等第一个 Zigbee / Matter 设备到了再做。

## 计划

1. HA + 米家接入，验证洗衣机（米家双区洗衣机 Pro 双洗烘 10kg）能不能远程预约 ✓（2026-10-22）
2. 一键约洗衣机（上下桶「自定义」，6 点前洗完，按实测用时留余量）← 已上线，待第一次真实预约验证
3. 自己的手机 App ✓；外网访问 ✓；米家式全屋首页和设备详情页 ✓（2026-10-22）。还差：报警推送（B）、摄像头（C）、场景（D）
4. 语音链路：SenseVoice 识别（香橙派，可用 NPU）→ 大模型 → TTS，先用手机麦克风跑通
5. 小爱音箱：型号支持就刷 open-xiaoai 接到自己的后端，否则用 mi-gpt 过渡
6. ESP32-S3 按键拾音器
