"""Run a real-browser console walkthrough against disposable demo data.

Install Playwright separately; this script never deletes existing projects.
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
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)

    def api(path, data=None):
        req = Request(
            args.api_url.rstrip("/") + path,
            data=json.dumps(data).encode() if data is not None else None,
            headers={"Content-Type": "application/json"},
        )
        with urlopen(req, timeout=10) as response:
            return json.load(response)

    def wait_candidate(predicate):
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            for candidate in api(f"/api/projects/{pid}/memory/candidates"):
                if predicate(candidate):
                    return candidate
            time.sleep(0.5)
        raise AssertionError("Candidate did not reach expected state")

    name = "Console acceptance " + uuid4().hex[:6]
    errors = []
    with sync_playwright() as playwright:
        options = {"headless": True}
        if args.browser_executable:
            options["executable_path"] = args.browser_executable
        browser = playwright.chromium.launch(**options)
        page = browser.new_page(viewport={"width": 1440, "height": 1050})
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(args.url)
        page.get_by_label("Create a project", exact=True).fill(name)
        page.get_by_role("button", name="Add workspace +").click()
        expect(page.get_by_role("heading", name=name, exact=True)).to_be_visible()
        pid = next(p["id"] for p in api("/api/projects") if p["name"] == name)
        page.get_by_role("tab", name="Context explorer", exact=True).click()
        page.get_by_label("SOURCE CONTENT").fill(
            "Decision[storage]: Use SQLite for the project database."
        )
        page.get_by_role("button", name="Import document").click()
        page.get_by_role("button", name="architecture.md").click(timeout=60000)
        expect(
            page.get_by_label("Memory inspector")
            .get_by_text("Decision[storage]: Use SQLite for the project database.", exact=True)
            .first
        ).to_be_visible()
        page.screenshot(path=str(output / "01-context.png"), full_page=True)
        wait_candidate(lambda c: c["title"] == "storage" and c["status"] == "pending")
        page.get_by_role("tab", name="Memory review", exact=True).click()
        page.get_by_role("button", name="Accept candidate", exact=True).click(timeout=60000)
        accepted = wait_candidate(lambda c: c["title"] == "storage" and c["status"] == "applied")
        page.get_by_role("button", name="Open resulting memory").click(timeout=15000)
        inspector = page.get_by_label("Memory inspector")
        inspector.get_by_text("Correct this memory", exact=True).click()
        inspector.get_by_label("NEW STATEMENT").fill(
            "Use PostgreSQL with pgvector for the project database."
        )
        inspector.get_by_role("button", name="Propose correction", exact=True).click()
        correction = wait_candidate(lambda c: c["action"] == "UPDATE" and c["status"] == "pending")
        card = page.locator(".candidate").filter(has_text="Use PostgreSQL with pgvector")
        card.get_by_role("button", name="Accept candidate", exact=True).click(timeout=15000)
        wait_candidate(lambda c: c["id"] == correction["id"] and c["status"] == "applied")
        page.get_by_role("button", name="Refresh", exact=False).click()
        expect(inspector.locator("pre").first).to_have_text(
            "Use PostgreSQL with pgvector for the project database."
        )
        inspector.get_by_text("Version comparison", exact=False).click()
        page.screenshot(path=str(output / "02-memory.png"), full_page=True)
        page.get_by_role("tab", name="Conversation", exact=True).click()
        page.get_by_role("button", name="New conversation +").click()
        page.get_by_label("MESSAGE", exact=True).fill(
            "What project database uses PostgreSQL and pgvector?"
        )
        page.get_by_role("button", name="Send message").click()
        expect(page.get_by_role("status").filter(has_text="working")).to_have_count(
            0, timeout=60000
        )
        page.locator(".message.assistant .citation", has_text="storage.md").click(timeout=60000)
        expect(inspector.locator("pre").first).to_have_text(
            "Use PostgreSQL with pgvector for the project database."
        )
        page.reload()
        expect(page.locator(".message.assistant")).to_contain_text("Offline demo", timeout=15000)
        expect(page.get_by_label("Run trace")).to_contain_text("succeeded")
        page.screenshot(path=str(output / "03-chat-trace.png"), full_page=True)
        # Exercise the destructive UI only on this script's new disposable memory.
        page.locator(".message.assistant .citation", has_text="storage.md").click()
        inspector.get_by_text("Forget this source", exact=True).click()
        inspector.get_by_role("button", name="Preview affected memories").click()
        inspector.get_by_label("I understand the source history will be redacted").check()
        inspector.get_by_role("button", name="Forget permanently").click()
        expect(inspector).to_contain_text("retracted", timeout=15000)
        assert api(f"/api/memory/{accepted['node_id']}")["content"] == ""
        page.set_viewport_size({"width": 390, "height": 844})
        page.screenshot(path=str(output / "04-mobile.png"), full_page=True)
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
                    "import",
                    "review",
                    "correction",
                    "versions",
                    "cross-session citation",
                    "reload",
                    "trace",
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
