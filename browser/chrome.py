"""专用 Chrome 会话：Playwright + 持久化 Profile（登录态可复用）。

设计原则（对应 POC 的安全要求）：
1. 只使用独立的 profile 目录，不复用、不读取用户日常 Chrome 的任何数据。
2. 不做任何反检测/隐身化处理（不加 AutomationControlled 之类的规避参数）。
3. 不接管登录流程：遇到登录页就停下来，等人手动登录。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from playwright.sync_api import Browser, BrowserContext, Page, Playwright, sync_playwright

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PROFILE_DIR = PROJECT_ROOT / ".chrome-profile"
DEFAULT_VIEWPORT = {"width": 1440, "height": 900}
DEFAULT_LOCALE = "zh-CN"
DEFAULT_TZ = "Asia/Shanghai"

# 仅使用普通浏览器参数，不含任何反爬/隐身参数
CHROME_ARGS = [
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-features=Translate",
]


class BrowserSetupError(RuntimeError):
    """浏览器启动失败（通常是 Chrome 不存在或 profile 被占用）。"""


@dataclass
class BrowserSession:
    playwright: Playwright
    context: BrowserContext
    page: Page
    profile_dir: Path
    persistent: bool
    browser: Browser | None = None

    def close(self) -> None:
        """逆序关闭。持久化 context 关闭即退出浏览器。"""
        errors: list[str] = []
        try:
            if self.persistent:
                self.context.close()
            else:
                if self.browser is not None:
                    self.browser.close()
                else:
                    self.context.close()
        except Exception as exc:  # noqa: BLE001
            errors.append(f"close context: {exc}")
        try:
            self.playwright.stop()
        except Exception as exc:  # noqa: BLE001
            errors.append(f"stop playwright: {exc}")
        if errors:
            print(f"[warn] 关闭浏览器时出现问题：{'; '.join(errors)}", file=sys.stderr)

    def __enter__(self) -> "BrowserSession":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def start_browser(
    profile_dir: str | Path | None = None,
    headless: bool = False,
    persistent: bool = True,
    use_real_chrome: bool = True,
) -> BrowserSession:
    """启动浏览器并返回会话。

    persistent=True 时使用 launch_persistent_context：Cookies、登录态、localStorage
    都会写进 profile_dir，下次启动仍在，因此微博只需手动登录一次。
    """
    profile = Path(profile_dir) if profile_dir else DEFAULT_PROFILE_DIR
    profile.mkdir(parents=True, exist_ok=True)

    pw = sync_playwright().start()
    launch_kwargs: dict = {
        "headless": headless,
        "viewport": DEFAULT_VIEWPORT,
        "locale": DEFAULT_LOCALE,
        "timezone_id": DEFAULT_TZ,
        "args": CHROME_ARGS,
    }
    if use_real_chrome:
        # channel="chrome" 使用本机安装的 Chrome，而不是 Playwright 自带的 Chromium
        launch_kwargs["channel"] = "chrome"

    try:
        if persistent:
            context = pw.chromium.launch_persistent_context(str(profile), **launch_kwargs)
            page = context.pages[0] if context.pages else context.new_page()
            return BrowserSession(
                playwright=pw,
                context=context,
                page=page,
                profile_dir=profile,
                persistent=True,
            )
        browser = pw.chromium.launch(**{k: v for k, v in launch_kwargs.items() if k != "viewport"})
        context = browser.new_context(
            viewport=launch_kwargs["viewport"],
            locale=DEFAULT_LOCALE,
            timezone_id=DEFAULT_TZ,
        )
        page = context.new_page()
        return BrowserSession(
            playwright=pw,
            context=context,
            page=page,
            profile_dir=profile,
            persistent=False,
            browser=browser,
        )
    except Exception as exc:  # noqa: BLE001
        try:
            pw.stop()
        except Exception:  # noqa: BLE001
            pass
        hint = (
            "请确认：1) 本机已安装 Google Chrome；2) 没有另一个实例正在使用同一个 "
            f"profile 目录（{profile}）；3) 如未安装 Chrome，可改用 --no-real-chrome 并先执行 "
            "`uv run playwright install chromium`。"
        )
        raise BrowserSetupError(f"浏览器启动失败：{exc}\n{hint}") from exc
