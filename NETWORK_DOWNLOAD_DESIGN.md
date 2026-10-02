# B5 第一期：受控 HTTPS 文件下载

本期增加 `download_file(url, sha256, path)`，用于获取有明确来源和内容摘要的依赖文件。
Podman 的 network=none、实际内核检查、无宿主挂载和验收后写回保持原有机制。
下载由可信控制面中的隔离 Python 子进程完成；模型不能传入 shell、请求方法、请求头、凭据或代理。
文件先通过 SHA256 校验，再作为数据写入当前容器，沿用 Worker/交互候选的独立验收与发布。

## 授权与边界

- 默认白名单为空，不注册下载工具；配置只接受精确 DNS 主机名，不接受通配符、IP 或 URL。
- 只支持 HTTPS、443、无 userinfo/query/fragment 的 URL；仅 GET，不发送 body、Cookie 或认证头。
- 交互默认逐次审批，串行询问；非交互默认拒绝。受信自动化可显式设置 allowlist 授权模式。
  白名单是目的地限制，allowlist 模式是操作者的持续下载授权，二者不是模型参数。
- 每次解析 DNS 必须所有地址均为公网单播地址；拒绝 loopback/private/link-local/reserved 和 IPv6 转换地址。
  连接固定到本次检查的 IP，TLS SNI/证书仍按原域名校验；不让 HTTP 客户端再次按域名解析。
- 重定向每跳重新检查 URL、白名单和 DNS；限制跳数。允许列表中所有主机都可能成为跳转目的地。
- 固定字节、总时限和输出上限；超时/取消终止并等待可信下载子进程，不留下后台下载/写者。
  不读取代理环境变量或宿主 API 凭据，不把响应正文写进模型输出/审计。
- 目标是规范的容器内相对路径，禁止控制路径、链接与覆盖不同内容的已有文件；同路径同摘要可复用。
  内容不符、不安全路径、未授权或网络失败时不导入数据。
- 下载授权不放行 run_command 的联网命令、pip/npm 在线安装、POST、发布或通用 TCP。
  下载文件的真实性依赖审批者确认来源/摘要；域名允许并不保证内容可信，也不保证不会泄露模型已知数据。

## 审计与失败语义

下载开始与结束分别记录 network_download 事件：意图 URL、摘要、授权方式、状态，以及完成时可验证的
跳转/IP/HTTP 状态、字节数。不保存正文或任意远端错误文本。开始记录是意图，不等于请求已到达服务器。
审计意图与结束记录的等待各有 5 秒上限；写者异常或卡死时，意图未完成就不发请求。
取消或中断不能撤销已发出的 GET；候选回滚只丢弃本地下载成果，没有外部 exactly-once 保证。
控制面下载能力的白名单在应用层执行；本期没有给容器开放网络，也没有实现通用联网容器出口防火墙。

## 验收

1. 未配置/非白名单/未批准均不连接；大小、hash、URL 和目标路径非法不发布数据。
2. DNS 混合公网/私网、重绑定、重定向到非法目的地、跳转循环、TLS 证书失败均拒绝。
3. 有 Content-Length 和无 Content-Length 的超限体均受限；总超时/取消无后台子进程与写回。
4. 授权下载写进实际 Podman 容器，shell 直接联网仍失败；hash 错误时已有文件保留。
5. /task 与普通交互验证接受/拒绝：只接受被独立验收通过的下载成果，原 env/控制面不进入容器。
6. Windows 单元与静态检查单独记录；Linux 全量和真容器验收仅在独立 Ubuntu VM 新目录进行。

当前 B5 整项继续未完成；本期交付受控下载，后续是否开放更广联网取决于具体包管理/服务需求。

## 实现状态与源码

独立 Ubuntu VM **562 passed、1 skipped**，27 个真容器用例通过；Windows **435 passed、128 skipped**，两端 Ruff / Pyright 通过。证据与分层说明见 LINUX_SANDBOX_ACCEPTANCE.md 末节。
`execution/download.py` 负责审批/有限子进程/审计；`execution/download_helper.py` 负责 URL/DNS/IP 固定/TLS/跳转/字节校验；
`tool/builtin/download_file.py` 绑定当前执行域；`tool/sandbox_download_helper.py` 用 dirfd/O_NOFOLLOW 原子创建二进制数据。
Session 仅在配置白名单时注册工具；Worker 与普通交互复用同一控制面下载能力，成果沿原有候选验收发布。

实现参考 Python 官方 [HTTPSConnection](https://docs.python.org/3/library/http.client.html)、
[SSL 默认证书校验](https://docs.python.org/3/library/ssl.html) 与
[IP 地址分类](https://docs.python.org/3/library/ipaddress.html)。额外保守拒绝部分转换/特殊网段，以兼容不同 Python 的 IANA 分类版本。
