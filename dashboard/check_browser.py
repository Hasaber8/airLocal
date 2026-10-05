import pathlib
import re
from urllib.request import urlopen

from playwright.sync_api import expect, sync_playwright


ROOT = pathlib.Path(__file__).resolve().parent
URL = "http://127.0.0.1:8765"


def main():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000}, device_scale_factor=1)
        failures = []
        requests = []
        page.on("pageerror", lambda failure: failures.append(str(failure)))
        page.on("request", lambda request: requests.append(request.url))
        page.goto(URL)
        expect(page.locator("#stat-count")).to_have_text(re.compile(r"^[1-9]\d*$"))
        data = page.request.get(URL + "/api/state").json()
        assert data["reader"]["paired"] and data["reader"]["peerId"]
        expect(page.locator("#device-state")).to_have_text("Laptop paired")
        history = data["health"]["history"]
        assert data["health"]["bleOnly"] and history["source"] == "ble_capture_history"
        assert history["sleep"] == []
        live = data["health"]["live"]
        page.locator("#health-date").select_option(live["date"])
        expect(page.locator("#health-steps")).to_have_text("{:,}".format(live["steps"]))
        expect(page.locator("#health-distance")).to_have_text(f"{live['distanceKm']:.2f}")
        assert page.locator("#import-button").count() == 0
        assert "not yet decodable" in page.locator("#sleep-empty").inner_text()
        assert "BLE-only" in page.locator("#health-source").inner_text()
        heart_available = any(sample["date"] == live["date"] and sample["heartRate"] is not None
                              for sample in data["health"]["samples"])
        assert page.locator("#heart-chart svg").count() == (1 if heart_available else 0)
        if not heart_available:
            assert "No BLE heart-rate measurements" in page.locator("#heart-chart").inner_text()
        if data["health"]["live"]["steps"]:
            assert "BLE capture" in page.locator("#steps-chart title").last.text_content()
        page.screenshot(path=ROOT / "health-desktop.png", full_page=True)
        page.locator("#library > summary").click()
        page.wait_for_selector(".hex-row")
        count = int(page.locator("#stat-count").inner_text())
        assert page.locator("#capture-rows tr").count() == count
        assert "opaque envelope" in page.locator(".technical-note").inner_text()
        assert "stored-dtls-key" not in page.locator("#capture-rows").inner_text()
        selected = page.locator("#inspect-subtitle").inner_text()
        observed = next(item["observedEnvelope"] for item in data["captures"] if item["id"] == selected)
        assert page.locator(".header-byte").count() == (21 if observed else 0)
        page.screenshot(path=ROOT / "desktop.png", full_page=True)

        page.locator("#type-filter").select_option("sensor")
        assert page.locator("#capture-rows tr").count() > 0
        assert all("Sensor" in text for text in page.locator("#capture-rows tr").all_inner_texts())
        page.locator("#capture-rows tr").first.click()
        expect(page.locator("#inspect-title")).to_have_text("Sensor capture")
        page.locator("#next-page").click()
        expect(page.locator("#byte-range")).to_have_text(re.compile(r"^256"))
        page.locator("#previous-page").click()
        expect(page.locator("#byte-range")).to_have_text(re.compile(r"^0"))
        href = page.locator("#download-link").get_attribute("href")
        with page.expect_download() as event:
            page.locator("#download-link").click()
        with urlopen(URL + href) as response:
            assert pathlib.Path(event.value.path()).read_bytes() == response.read()

        page.locator("#search-input").fill("no-such-capture")
        assert page.locator("#library-empty").is_visible()
        page.locator("#search-input").fill("")
        page.locator("#type-filter").select_option("all")
        submitted = []
        job = {"id": "browser-check", "kind": "sensor", "status": "running", "startedAt": 1,
               "finishedAt": None, "captureId": None, "log": "Simulated reader job for browser checks"}
        setup_missing = False

        def read_request(route):
            submitted.append(route.request.post_data_json)
            route.fulfill(status=202, json=job)

        def state_request(route):
            response = route.fetch()
            data = response.json()
            data["job"] = job
            if setup_missing:
                data["reader"].update(available=False, paired=False, peerId=None)
            route.fulfill(response=response, json=data)

        page.route("**/api/read", read_request)
        page.route("**/api/state", state_request)
        page.locator("[data-kind=sensor]").click()
        page.locator("#read-button").click()
        expect(page.locator("#job-status")).to_have_text("RUNNING")
        assert submitted == [{"kind": "sensor"}]
        assert page.locator("#read-button").is_disabled()
        job.update(status="failed", log="Simulated Bluetooth connection timeout")
        expect(page.locator("#job-status")).to_have_text("FAILED")
        assert "timeout" in page.locator("#job-log").inner_text()
        assert page.locator("#read-button").is_enabled()

        setup_missing = True
        page.evaluate("refresh()")
        expect(page.locator("#device-state")).to_have_text("Setup needed")
        for control in ("#read-button", "#live-button", "#record-button"):
            assert page.locator(control).is_disabled()
        assert "pair --device UUID" in page.locator("#read-hint").inner_text()
        setup_missing = False
        page.evaluate("refresh()")
        expect(page.locator("#device-state")).to_have_text("Laptop paired")

        page.set_viewport_size({"width": 390, "height": 844})
        page.screenshot(path=ROOT / "mobile.png", full_page=True)
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        assert not failures, failures
        assert all(request.startswith(URL + "/") for request in requests), requests
        page.unroute_all(behavior="wait")
        browser.close()
        print(f"Browser checks passed: BLE-only health data, no Fitbit imports, {count} standalone captures, filtering, byte paging, download, simulated controls, and mobile layout")


if __name__ == "__main__":
    main()
