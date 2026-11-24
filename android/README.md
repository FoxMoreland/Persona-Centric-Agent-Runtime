# PCAR 安卓开发

此工程通过原生 Android 宿主加载社区版 Python 核心。阶段一的验收应用检查 SQLite、Keystore、回环 HTTP/TLS、Flash 分词预算和 BGE 召回。阶段二的工作台使用同一套运行时，提供会话、人物和设置三个竖屏栏目。

1.4.0 候选可从人物菜单导出角色、桌面历史和已整理记忆。通过系统文件选择器导入 `.pcarpack`，核对用途后接续聊天。自备 Jev 可在「设置 → 决策模型设置」检查连接和 Choice 功能，通过后启用，再在聊天页授权该接收方。默认仍用一把 DeepSeek 官方 Key。

两个应用均由 Gradle 构建，分别提供 x86_64 和 arm64-v8a 调试变体。编译 SDK 为 36，声明最低 API 为 31。回环测试证书和阶段一参考语料只随验收应用提供。

## 实现

- Python 3.12 由 Chaquopy 17.2.0 提供。HTTP 依赖使用锁定的通用 Python wheel。SQLite 使用运行时内置版本。
- BGE 使用原版 ONNX 模型与 Android ONNX Runtime 1.31.1。Rust Tokenizers 为 BGE 提供 token 和原文偏移，为 Flash 建卡提供精确计数。两端复用现有切片、预算和召回排序规则。
- Windows 向量运算继续使用 NumPy。Android 路径使用标准库的 float32 数组，沿用小端缓存格式。Android 本机服务由单个应用进程在实例文件锁内持有，进程标识随宿主重启更新。
- Android Keystore 使用 AES-GCM 加密本机凭据。Windows 实例沿用现有 DPAPI 数据格式。

## 准备外部工具和测试资产

在工程外建立工具缓存。Gradle 8.13、JDK 17 或 21、Android SDK 36 和 Build Tools 36.1.0 用于构建。Rust 1.100.0 配置 x86_64-linux-android 与 aarch64-linux-android 目标，使用 NDK r30 和 Visual Studio 的 x64 开发环境构建 JNI。Gradle 缓存、Cargo 依赖、只读模型资产、APK 和测试记录保存在工程外。

按 [requirements.txt](requirements.txt) 准备通用 wheel，放入工具缓存的 wheels 目录。首次依赖解析使用 Windows 已配置的手动代理。安装 Rust 目标并按 [Cargo.lock](native/Cargo.lock) 下载依赖后，原生库和 APK 可离线构建。

用 Windows 社区版 1.0 的内嵌 Python 执行 [prepare_stage1.py](tools/prepare_stage1.py)。参数指向该发行包的 BGE 文件、已校验的 ONNX Runtime AAR 和许可证，以及上一轮模拟器实验的 fixture.json 和 windows.json。输出选择工具缓存下的 prepared 目录。该步骤重新计算 Windows Flash 参考计数，并将两套参考材料一同提供给 APK。

在 prepared/assets/stage1 生成 loopback.crt 与 loopback.key。这是仅限 127.0.0.1 的自签名测试证书，可使用下面的 OpenSSL 命令。在验收应用中分别核对默认信任库拒绝该证书和显式信任后成功连接。

```powershell
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 365 -subj '/CN=PCAR Stage1 Loopback Test' -addext 'subjectAltName=IP:127.0.0.1' -addext 'basicConstraints=critical,CA:TRUE' -out loopback.crt -keyout loopback.key
```

## 构建与验收命令

下列变量由操作者指定。工具脚本接收绝对路径。执行位置为 android 目录。

```powershell
./tools/build-native.ps1 -ToolCache $ToolCache -Ndk $Ndk -VsDevShell $VsDevShell
$build = @{Sdk=$Sdk; Jdk=$Jdk; Gradle=$Gradle; BuildPython=$BuildPython; ToolCache=$ToolCache; Output=$ApkDir}
./tools/build-stage1.ps1 @build
python -B ./tools/verify_apks.py --apk-dir $ApkDir --zipalign "$Sdk/build-tools/36.1.0/zipalign.exe" --output "$EvidenceDir/apk-verification.json"
$check = @{Adb="$Sdk/platform-tools/adb.exe";Serial=$Serial;ExpectedAvd=$Avd;PageSize=$PageSize;Apk=$Apk;Output=$EvidenceDir}
./tools/check-stage1.ps1 @check
```

Gradle 缓存首次准备时，为构建命令加上 RefreshDependencies 开关。验收脚本按指定模拟器名称和内存页大小核对连接，断网后安装调试包并导出阶段报告。真机功能与性能验收使用后续完整 PCAR.apk。

## 构建和使用安卓工作台

沿用上面的工具缓存、模型和原生库，执行下面的命令。首次构建解析 AndroidX WebKit 时使用 RefreshDependencies 开关。

```powershell
./tools/build-workbench.ps1 @build
python -B ./tools/verify_apks.py --apk-dir $ApkDir --prefix PCAR-Android-Stage3 --zipalign "$Sdk/build-tools/36.1.0/zipalign.exe" --output "$EvidenceDir/apk-verification.json"
```

工作台以应用内的 HTTPS 来源加载打包页面。桥接只接收主页面的指定操作，本机服务监听回环地址。Key 在原生对话框填写，使用 Android Keystore 加密。工作台只显示保存状态。

- 人物页的「新建角色」可以通过系统选择器读取聊天 Markdown 或角色 JSON。选取 Markdown 后进入说话人确认。选取角色 JSON 后可核对资料并保存。
- 安卓返回键先关闭当前工作台对话框，随后返回会话。应用切到后台时暂停工作台轮询和新工作，回到前台续用已启动的本机服务。系统结束进程后，从本机记录恢复草稿和导入进度。
- 工作台调试包的数据保存在自己的应用私有目录。ARM64 真机使用后续完整 PCAR.apk。

## 第三阶段验收

- 工作台读取 Android 系统 HTTP 代理。没有代理时沿用社区版对官方 DeepSeek HTTPS 的直连规则。模拟器的正式建卡验收使用 Windows 手动代理提供的 HTTP 连接。
- 用户启用长期记忆后，维护队列在应用前台运行。切到后台停止取新任务，已收回执按现有恢复流程保留。
- 构建命令加上 BuildAcceptance 开关，可生成独立的 x86_64 验收 APK。它复用仓库已有的建卡、历史接续、记忆和联网授权测试，并提供模拟器专用的一次性 Key 配置。工作台 APK 只包含产品代码。
- 实际聊天导出可用 `android.tools.select_card_excerpt` 按已授权的说话人和轮数截取。验证各条记录可按原文回读后，再在工作台内确认资料范围和使用已审模板。遇到已派发且结果未知的请求，先核对原回执。
