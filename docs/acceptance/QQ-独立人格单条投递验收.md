# 独立人格 QQ 单条投递验收

本验收由新建的本地 PCAR 人格向指定接收号发送一条带唯一编号的纯文本。接收人在手机 QQ 核对发件号和完整编号。桥接候选为用户提供的 SnowLuma 2.2.0 Windows x64 脱敏包，使用本机 OneBot v11 HTTP 和 `send_msg` 私聊动作。模型调用为零。

## 首次准备

在源码根目录中使用 Python 3.12 和仓库锁定的依赖。选择源码之外一个尚不存在的绝对路径作为验收目录。已有的验收目录从 `status` 续接。

```powershell
python -m persona_runtime.qq_one_shot --home $testHome init
python -m persona_runtime.qq_one_shot --home $testHome status
```

首次绑定时提交用户指定的压缩包及其 SHA-256。预检固定匹配 OneBot 接口报告的 `SnowLuma` 和 `2.2.0-node`。账号使用测试登录号和测试接收号，端口使用该独立客户端的回环端口。绑定后直接沿该记录续接。

```powershell
python -m persona_runtime.qq_one_shot --home $testHome bind --sender $testSender --recipient $testRecipient --port $localPort --client-artifact $verifiedArtifact --client-sha256 $verifiedSha256
```

## 预检、投递和收件

用户手动完成客户端登录。配置客户端的 OneBot HTTP 监听地址为 `127.0.0.1`，启用 Bearer 鉴权，并关闭入站事件转发。命令在控制台隐藏输入令牌。预检核对无令牌请求被拒绝、有令牌请求返回版本、协议为 v11、登录账号与绑定一致以及客户端在线。

```powershell
python -m persona_runtime.qq_one_shot --home $testHome preflight
```

预检通过且确认此次发送授权后，仅执行一次以下命令。发送会重新预检并先持久化尝试，再派发纯文本。

```powershell
python -m persona_runtime.qq_one_shot --home $testHome send
```

接收方在手机 QQ 对应私聊核对完整编号后，用返回的尝试编号登记收件。

```powershell
python -m persona_runtime.qq_one_shot --home $testHome confirm-received --code $observedCode
```

| 状态 | 含义 |
| --- | --- |
| `awaiting_recipient_confirmation` | 平台返回消息 ID，等接收方核对 |
| `received` | 接收方已核对，桥接回执齐全 |
| `received_without_bridge_receipt` | 接收方已核对，桥接消息 ID 缺失 |
| `delivery_uncertain` | 检查原回执和接收端，保留原尝试 |

遇风控、下线、账号不符、发送错误或状态未知时停止外发。验收目录保存一次尝试，进程退出后可用 `status` 读回。确认结束后关闭本轮桥接端口，账号与回执保留在本地验收目录。
