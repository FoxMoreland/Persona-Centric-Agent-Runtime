"""A private WebView2 window for visiting DeepSeek and receiving the official export."""
import os
from threading import Thread
from time import monotonic
from urllib.parse import urlsplit
import webbrowser

from .deepseek_intake import DeepSeekInbox
from .models import RuntimeErrorBase
from .network_proxy import configured_proxy


SITE = "https://chat.deepseek.com/"
CHAT_HOST = "chat.deepseek.com"
ASSET_HOSTS = {CHAT_HOST, "cdn.deepseek.com", "fe-static.deepseek.com"}
VERIFICATION_HOSTS = {"a0fea896111e.39f92532.us-west-1.token.awswaf.com"}
HCAPTCHA_SCRIPT = {"js.hcaptcha.com"}
HCAPTCHA_ASSETS = {"newassets.hcaptcha.com"}
HCAPTCHA_API = {"api.hcaptcha.com", "api2.hcaptcha.com", "hcaptcha.com"}
HCAPTCHA_MEDIA = {"imgs.hcaptcha.com", "imgs3.hcaptcha.com"}
TURNSTILE_HOSTS = {"challenges.cloudflare.com"}
TURNSTILE_PLATFORM = "/cdn-cgi/challenge-platform/"
LOAD_TIMEOUT = 30


def web_route():
    proxy = configured_proxy("https")
    if proxy:
        return "--proxy-server=" + proxy + " --proxy-bypass-list=<-loopback>"
    from pathlib import Path
    if os.environ.get("PCAR_EDITION") == "community" or Path(__file__).with_name("community-edition").is_file():
        return "--no-proxy-server"
    raise RuntimeErrorBase("请开启 Windows 手动系统代理后打开内置浏览器。")


def approved(url, hosts):
    try:
        parsed = urlsplit(url)
        return parsed.scheme == "https" and parsed.hostname in hosts and parsed.port in (None, 443) and not (
            parsed.username or parsed.password)
    except ValueError:
        return False


def hcaptcha_frame(url):
    if not approved(url, HCAPTCHA_ASSETS):
        return False
    path = urlsplit(url).path
    return path.startswith("/captcha/v1/") and path.endswith("/static/hcaptcha.html")


def turnstile_frame(url):
    if not approved(url, TURNSTILE_HOSTS):
        return False
    path = urlsplit(url).path
    return path.startswith(TURNSTILE_PLATFORM) and "/turnstile/" in path


def approved_frame(url):
    return (url in ("about:blank", "about:srcdoc") or approved(url, {CHAT_HOST})
            or hcaptcha_frame(url) or turnstile_frame(url))


def turnstile_resource(url, context, method):
    if not approved(url, TURNSTILE_HOSTS):
        return False
    path = urlsplit(url).path
    if context == "Document":
        return method == "GET" and turnstile_frame(url)
    if path.startswith("/turnstile/v0/"):
        return context == "Script" and method == "GET"
    if not path.startswith(TURNSTILE_PLATFORM):
        return False
    if context in ("XmlHttpRequest", "Fetch"):
        return method in ("GET", "POST", "OPTIONS")
    return method == "GET" and context in ("Script", "Stylesheet", "Image", "Font", "Media", "Other")


def hcaptcha_resource(url, context, method):
    """Keep the verification service separate from page and download origins."""
    if approved(url, HCAPTCHA_SCRIPT):
        return method == "GET" and context == "Script" and urlsplit(url).path == "/1/api.js"
    if approved(url, HCAPTCHA_ASSETS):
        if context == "Document":
            return method == "GET" and hcaptcha_frame(url)
        return method == "GET" and urlsplit(url).path.startswith(("/captcha/v1/", "/c/"))
    if approved(url, HCAPTCHA_API):
        endpoint = urlsplit(url).path.removeprefix("/").partition("/")[0]
        return context in ("XmlHttpRequest", "Fetch") and method in ("POST", "OPTIONS") and (
            endpoint in ("checksiteconfig", "getcaptcha", "checkcaptcha")
        )
    return (approved(url, HCAPTCHA_MEDIA) and method == "GET"
            and context in ("Image", "Media", "XmlHttpRequest", "Fetch"))


def approved_resource(url, context, method):
    if approved(url, ASSET_HOSTS) or hcaptcha_resource(url, context, method) or turnstile_resource(url, context, method):
        return True
    if not approved(url, VERIFICATION_HOSTS):
        return False
    # The site's AWS WAF integration uses its own origin. Keep its access
    # limited to the verification resources the chat document requests.
    endpoint = urlsplit(url).path.rsplit("/", 1)[-1]
    if context == "Script":
        return method == "GET" and endpoint == "challenge.js"
    return context in ("XmlHttpRequest", "Fetch") and (
        (endpoint == "inputs" and method in ("GET", "OPTIONS"))
        or (endpoint == "mp_verify" and method in ("POST", "OPTIONS"))
    )


class ExportBrowser:
    def __init__(self, owner, paths, notice):
        from Microsoft.Web.WebView2.WinForms import CoreWebView2CreationProperties, WebView2
        from System.Windows.Forms import Button, DockStyle, FlowLayoutPanel, Form, Label, Timer

        arguments = web_route()
        self.paths = paths
        self.notice = notice
        self.inbox = DeepSeekInbox(paths)
        self.form = Form()
        self.form.Text = "DeepSeek 官方数据导出"
        self.form.Width = 1060
        self.form.Height = 760
        self.view = WebView2()
        self.view.Dock = DockStyle.Fill
        config = CoreWebView2CreationProperties()
        config.UserDataFolder = str(paths.home / "cache/deepseek-browser")
        config.AdditionalBrowserArguments = arguments
        self.view.CreationProperties = config
        self.view.CoreWebView2InitializationCompleted += self._ready
        self.operations = {}
        self.closed = False
        self.document_loaded = False
        self.checking_page = False
        self.navigation_id = None
        self.load_deadline = 0
        self.page_timer = Timer()
        self.page_timer.Interval = 750
        self.page_timer.Tick += self._check_page
        self.status = Label()
        self.status.AutoSize = True
        self.status.Text = "正在启动内置浏览器…"
        toolbar = FlowLayoutPanel()
        toolbar.Dock = DockStyle.Top
        toolbar.Height = 38
        for label, callback in (("用系统浏览器打开", lambda: webbrowser.open(SITE)),
                                ("重新加载", self._reload),
                                ("清除网页登录", self._clear_login)):
            button = Button()
            button.Text = label
            button.AutoSize = True
            button.Click += lambda sender, event, action=callback: action()
            toolbar.Controls.Add(button)
        toolbar.Controls.Add(self.status)
        self.form.Controls.Add(self.view)
        self.form.Controls.Add(toolbar)
        self.form.FormClosed += self._close
        self.form.Show(owner)
        self.view.EnsureCoreWebView2Async(None)

    def _ready(self, sender, args):
        if not args.IsSuccess:
            self.status.Text = "内置浏览器启动失败。可用系统浏览器导出。"
            return
        from Microsoft.Web.WebView2.Core import CoreWebView2WebResourceContext, CoreWebView2WebResourceRequestSourceKinds

        core = sender.CoreWebView2
        settings = core.Settings
        settings.AreHostObjectsAllowed = False
        settings.IsWebMessageEnabled = False
        settings.AreDevToolsEnabled = False
        settings.IsGeneralAutofillEnabled = False
        settings.IsPasswordAutosaveEnabled = False
        core.NavigationStarting += self._navigate
        core.NavigationCompleted += self._navigation_completed
        core.FrameNavigationStarting += self._frame_navigation
        core.NewWindowRequested += self._new_window
        core.WebResourceRequested += self._resource
        try:
            core.AddWebResourceRequestedFilter("*", CoreWebView2WebResourceContext.All,
                                               CoreWebView2WebResourceRequestSourceKinds.All)
        except (AttributeError, TypeError):
            self.status.Text = "当前 WebView2 无法隔离网页资源，请用系统浏览器导出。"
            return
        core.DownloadStarting += self._download
        self._reload()

    def _reload(self):
        core = self.view.CoreWebView2
        if core is not None:
            self.page_timer.Stop()
            core.Navigate(SITE)

    def _navigate(self, sender, args):
        if not approved(str(args.Uri), {CHAT_HOST}):
            args.Cancel = True
            self.navigation_id = None
            self.page_timer.Stop()
            self.status.Text = "此登录或导出步骤请在系统浏览器完成。"
            return
        self.navigation_id = args.NavigationId
        self.document_loaded = False
        if not self.page_timer.Enabled:
            self.load_deadline = monotonic() + LOAD_TIMEOUT
            self.status.Text = "正在加载 DeepSeek 官网…"
            self.page_timer.Start()

    def _frame_navigation(self, sender, args):
        if not approved_frame(str(args.Uri)):
            args.Cancel = True

    def _navigation_completed(self, sender, args):
        if args.NavigationId != self.navigation_id:
            return
        code = args.HttpStatusCode
        if not args.IsSuccess or code >= 400:
            self.page_timer.Stop()
            reason = f"HTTP {code}" if code >= 400 else str(args.WebErrorStatus)
            self.status.Text = f"页面加载失败（{reason}）。请重新加载或用系统浏览器。"
        elif code == 202:
            if self.page_timer.Enabled:
                self.status.Text = "正在等待官网访问验证…"
        else:
            self.document_loaded = True
            if not self.page_timer.Enabled:
                self.load_deadline = monotonic() + LOAD_TIMEOUT
                self.page_timer.Start()

    def _check_page(self, sender, args):
        if monotonic() >= self.load_deadline:
            self.page_timer.Stop()
            self.status.Text = "网页暂未显示。请重新加载或用系统浏览器。"
            return
        if not self.document_loaded or self.checking_page:
            return
        from System import Action, String
        from System.Threading.Tasks import Task, TaskScheduler

        navigation_id = self.navigation_id
        self.checking_page = True

        def checked(task):
            self.checking_page = False
            if self.closed or not self.page_timer.Enabled or navigation_id != self.navigation_id:
                return
            if not task.IsFaulted and not task.IsCanceled and task.Result == "true":
                self.page_timer.Stop()
                self.status.Text = "登录后在设置中选择数据导出。"

        script = "Boolean(document.body?.innerText.trim())"
        try:
            self.view.CoreWebView2.ExecuteScriptAsync(script).ContinueWith(
                Action[Task[String]](checked), TaskScheduler.FromCurrentSynchronizationContext())
        except Exception:
            self.checking_page = False

    def _new_window(self, sender, args):
        args.Handled = True
        if approved(str(args.Uri), {CHAT_HOST}):
            sender.Navigate(str(args.Uri))
        else:
            self.status.Text = "此登录或导出步骤请在系统浏览器完成。"

    def _resource(self, sender, args):
        if not approved_resource(str(args.Request.Uri), str(args.ResourceContext), str(args.Request.Method)):
            args.Response = sender.Environment.CreateWebResourceResponse(None, 403, "Forbidden", "")

    def _clear_login(self):
        if self.view.CoreWebView2 is not None:
            self.view.CoreWebView2.Profile.ClearBrowsingDataAsync()
            self.status.Text = "已请求清除该窗口的网页登录数据。"

    def _download(self, sender, args):
        from pathlib import Path
        from uuid import uuid4
        from .config import ConfigStore
        from .desktop_activation import DesktopActivation
        from .deepseek_intake import MAX_ARCHIVE_BYTES

        operation = args.DownloadOperation
        filename = Path(str(args.ResultFilePath)).suffix.lower()
        if (self.operations or not approved(str(operation.Uri), ASSET_HOSTS) or filename != ".zip"
                or operation.TotalBytesToReceive > MAX_ARCHIVE_BYTES
                or DesktopActivation(ConfigStore(self.paths)).status()["state"] != "verified"):
            args.Cancel = True
            self.status.Text = "这个下载无法自动接收。可在系统浏览器下载 ZIP。"
            return
        self.inbox.cache.mkdir(parents=True, exist_ok=True)
        target = self.inbox.cache / (uuid4().hex + ".zip")
        self.operations[target] = operation
        self.navigation_id = None
        self.page_timer.Stop()
        args.ResultFilePath = str(target)
        args.Handled = True
        self.status.Text = "正在接收官方导出 ZIP。"

        def progress(sender, event):
            if sender.BytesReceived > MAX_ARCHIVE_BYTES:
                sender.Cancel()

        def changed(sender, event):
            state = str(sender.State)
            if state not in ("Completed", "Interrupted") or self.operations.pop(target, None) is None:
                return
            if state == "Completed":
                self.status.Text = "已下载，正在核对 ZIP。"
                Thread(target=self._received, args=(target,), daemon=True).start()
            else:
                target.unlink(missing_ok=True)
                self.status.Text = "下载中断。可再次下载或选择已有 ZIP。"

        operation.BytesReceivedChanged += progress
        operation.StateChanged += changed

    def _received(self, target):
        from .deepseek_export import inspect
        try:
            inspect(target)
            self.inbox.enqueue(target, owned=True)
            self.notice("DeepSeek 导出已就绪。请在工作台选择会话。", True)
        except (ValueError, OSError):
            target.unlink(missing_ok=True)
            self.notice("这个文件无法作为 DeepSeek 官方导出导入。", False)

    def _close(self, sender, args):
        self.closed = True
        self.page_timer.Stop()
        self.page_timer.Dispose()
        for operation in tuple(self.operations.values()):
            operation.Cancel()
