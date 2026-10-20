"""Exercise folder import, memory review, chat and monitoring in a real browser.

This script creates its own disposable project and never deletes existing data.
Use an offline/demo-configured API and worker for deterministic acceptance.
"""

import argparse
import json
import time
from pathlib import Path
from urllib.request import Request, urlopen
from uuid import uuid4


def main():
    from playwright.sync_api import expect, sync_playwright

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8080")
    parser.add_argument("--api-url", default="http://127.0.0.1:8000")
    parser.add_argument("--browser-executable")
    parser.add_argument("--output", default=".artifacts/console")
    args = parser.parse_args()
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    name = "工作台验收 " + uuid4().hex[:6]
    source = output / name
    source.mkdir()
    (source / "architecture.md").write_text(
        "Decision[storage]: Use SQLite for the project database.", encoding="utf-8"
    )
    (source / ".env.json").write_text("excluded", encoding="utf-8")

    def api(path):
        with urlopen(Request(args.api_url.rstrip("/") + path), timeout=10) as response:
            return json.load(response)

    def wait_candidate(predicate):
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            for candidate in api(f"/api/projects/{pid}/memory/candidates"):
                if predicate(candidate):
                    return candidate
            time.sleep(0.5)
        raise AssertionError("Candidate did not reach expected state")

    errors = []
    with sync_playwright() as playwright:
        options = {"headless": True}
        if args.browser_executable:
            options["executable_path"] = args.browser_executable
        browser = playwright.chromium.launch(**options)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(args.url)
        page.get_by_role("button", name="新建项目", exact=True).click()
        dialog = page.get_by_role("dialog")
        dialog.get_by_role("button", name="导入文件夹", exact=True).click()
        dialog.get_by_label("选择项目文件夹", exact=True).set_input_files(str(source))
        dialog.get_by_label("项目名称", exact=True).fill(name)
        dialog.get_by_role("button", name="创建项目", exact=True).click()
        expect(page.get_by_label("项目资料", exact=True)).to_be_visible()
        pid = next(p["id"] for p in api("/api/projects") if p["name"] == name)
        expect(page.locator(".file-preview-list")).to_contain_text("architecture.md")
        expect(page.locator(".file-preview-list")).not_to_contain_text(".env.json")
        page.get_by_role("button", name="导入所选资料", exact=True).click()
        page.locator(".source-list").get_by_role(
            "button", name="architecture.md", exact=False
        ).click(timeout=60000)
        inspector = page.get_by_label("来源详情")
        expect(inspector.locator("pre").first).to_have_text(
            "Decision[storage]: Use SQLite for the project database."
        )
        page.screenshot(path=str(output / "01-sources.png"), full_page=True)
        wait_candidate(lambda c: c["title"] == "storage" and c["status"] == "pending")
        page.get_by_role("button", name="项目记忆", exact=True).click()
        page.get_by_role("button", name="待确认建议", exact=False).click()
        page.get_by_role("button", name="保存这条记忆", exact=True).click(timeout=15000)
        accepted = wait_candidate(lambda c: c["title"] == "storage" and c["status"] == "applied")
        page.get_by_role("button", name="已记住", exact=True).click()
        page.locator(".saved-memory").filter(has_text="SQLite").click(timeout=15000)
        inspector.get_by_text("修正这条记忆", exact=True).click()
        inspector.get_by_label("新的事实陈述").fill(
            "Use PostgreSQL with pgvector for the project database."
        )
        inspector.get_by_role("button", name="保存修改", exact=True).click()
        wait_candidate(lambda c: c["action"] == "UPDATE" and c["status"] == "applied")
        expect(inspector.locator("pre").first).to_have_text(
            "Use PostgreSQL with pgvector for the project database.", timeout=15000
        )
        inspector.get_by_text("版本对比", exact=False).click()
        page.screenshot(path=str(output / "02-memory.png"), full_page=True)
        page.get_by_role("button", name="关闭侧栏").click()
        page.get_by_label("模型", exact=True).select_option(label="evidence-demo · 离线演示")
        page.get_by_label("消息内容", exact=True).fill(
            "What project database uses PostgreSQL and pgvector?"
        )
        page.get_by_role("button", name="发送消息", exact=True).click()
        expect(page.locator(".message.user")).to_contain_text(
            "What project database", timeout=15000
        )
        citation = page.locator(".message.assistant .citation").filter(has_text="storage.md").first
        citation.click(timeout=60000)
        expect(inspector.locator("pre").first).to_have_text(
            "Use PostgreSQL with pgvector for the project database."
        )
        page.get_by_role("button", name="关闭侧栏").click()
        page.reload()
        expect(page.locator(".message.assistant")).to_contain_text("Offline demo", timeout=15000)
        page.screenshot(path=str(output / "03-conversation.png"), full_page=True)
        with page.expect_popup() as popup:
            page.get_by_role("link", name="项目监控", exact=True).click()
        monitor = popup.value
        expect(monitor.get_by_text("项目监控", exact=True)).to_be_visible()
        monitor.locator(".monitor-row").first.click(timeout=15000)
        expect(monitor.get_by_label("运行轨迹")).to_contain_text("调用耗时")
        expect(monitor.locator(".waterfall details").first).to_be_visible(timeout=15000)
        monitor.screenshot(path=str(output / "04-monitor.png"), full_page=True)
        monitor.close()
        # Destructive UI is exercised only on this script's newly created memory.
        page.locator(".message.assistant .citation").filter(has_text="storage.md").first.click()
        inspector.get_by_text("忘记此来源", exact=True).click()
        inspector.get_by_role("button", name="预览影响范围").click()
        inspector.get_by_label("我已知晓来源历史将被脱敏").check()
        inspector.get_by_role("button", name="确认忘记").click()
        expect(inspector).to_contain_text("已撤回", timeout=15000)
        assert api(f"/api/memory/{accepted['node_id']}")["content"] == ""
        page.get_by_role("button", name="关闭侧栏").click()
        page.set_viewport_size({"width": 390, "height": 844})
        page.wait_for_function(
            "document.querySelector('.sidebar').getBoundingClientRect().right <= 0"
        )
        page.screenshot(path=str(output / "05-mobile.png"), full_page=True)
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), (
            "mobile horizontal overflow"
        )
        assert not errors, errors
        browser.close()
    print(
        json.dumps(
            {
                "status": "passed",
                "project_id": pid,
                "checks": [
                    "folder snapshot",
                    "sensitive file filter",
                    "import",
                    "review",
                    "correction",
                    "versions",
                    "cross-thread citation",
                    "reload",
                    "independent monitor",
                    "forget",
                    "mobile",
                ],
                "screenshots": str(output),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
