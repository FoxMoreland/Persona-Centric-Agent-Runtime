# Windows 打包与 Authenticode 签名

Windows 社区版使用 Windows SDK 的 SignTool。签名构建依次签署 PCAR 启动程序、Inno Setup 安装器和卸载程序，保留随包第三方文件的原有签名。签名使用 SHA-256 和 RFC 3161 时间戳。

## 配置签名身份

Windows 证书库的“个人”存储需要有可用于代码签名的证书及其私钥访问权限。证书可由 USB 令牌或其他 Windows 密钥提供程序持有。当前用户证书库使用默认参数，本机证书库增加 --sign-machine-store。证书指纹用于选定发布者。密钥、PIN 和密码由本机密钥提供程序管理。

根据签发方的指引使用 RFC 3161 时间戳服务。用 Visual Studio 开发者终端运行现有的 Windows 构建命令，并增加以下参数：

    --signtool <Windows SDK 的 signtool.exe 路径>
    --sign-thumbprint <代码签名证书的 40 位十六进制指纹>
    --timestamp-url <签发方提供的 RFC 3161 URL>

参数按原构建命令的引号规则传入。签名或校验失败时构建停止。Windows 按 Authenticode 策略验证安装器、启动程序、卸载程序及其时间戳，随后在 build.json 记录签名状态、证书指纹和最终安装包的 SHA-256。正式发布使用 authenticode.state 为 verified 的产物，并在 Windows 上验证对上一公开版本的覆盖安装和旧数据读回。无签名参数的构建用于本地验证，报告状态为 unsigned。

加入签名后仍使用原 Windows 安装身份。发布签名构建时按版本规则分配新版本，校验值对应签名后的最终文件。
