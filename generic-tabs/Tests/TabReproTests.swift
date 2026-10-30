import XCTest
import UIKit

@MainActor
final class TabReproTests: XCTestCase {
    func testCombinedNativeHoldWindow() throws {
        continueAfterFailure = true
        let reproduced = try exerciseCombined("hold-combined")
        let decision = XCTAttachment(string: reproduced
            ? "Baseline observed multiple hittable selected native tabs while the real requested page was visible. Preserve finalized evidence before any candidate run."
            : "Baseline did not reproduce multiple selected native tabs. The candidate is not executed; no Atlas fix is established.")
        decision.name = "combined-native-conditional-candidate-decision"
        decision.lifetime = .keepAlways
        add(decision)
    }

    private func exerciseCombined(_ mode: String) throws -> Bool {
        let deadline = Date().addingTimeInterval(210)
        let app = XCUIApplication()
        app.launchEnvironment["REPRO_MODE"] = mode
        app.launch()
        defer { app.terminate() }
        XCTAssertTrue(app.staticTexts["page.0"].waitForExistence(timeout: 30))
        let identity = app.staticTexts["state.0"].label
        app.textFields["draft.0"].tap()
        app.textFields["draft.0"].typeText("Generic unsent draft")
        app.buttons["hide-keyboard"].tap()
        let startingDraft = app.textFields["draft.0"].value as? String ?? "<unavailable>"
        var reproduced = false
        for (step, target) in [1, 2, 3, 4, 5, 6, 0].enumerated() {
            guard Date() < deadline else {
                XCTFail("The bounded combined native arm expired.")
                return reproduced
            }
            let availableBars = app.tabBars.allElementsBoundByAccessibilityElement.filter {
                $0.exists && $0.isHittable && !$0.frame.isEmpty && $0.frame.intersects(app.frame)
            }
            XCTAssertEqual(availableBars.count, 1)
            let bar = try XCTUnwrap(availableBars.first)
            let y = (bar.frame.midY - app.frame.minY) / app.frame.height
            app.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: y))
                .press(forDuration: 0.7, thenDragTo: app.coordinate(withNormalizedOffset:
                    CGVector(dx: (CGFloat(target) + 0.5) / 14, dy: y)),
                    withVelocity: .slow, thenHoldForDuration: 0.4)
            let page = app.staticTexts["page.\(target)"]
            let appeared = page.waitForExistence(timeout: 5)
            let reached = appeared && page.isHittable
            let bars = app.tabBars.allElementsBoundByAccessibilityElement.filter {
                $0.exists && $0.isHittable && !$0.frame.isEmpty && $0.frame.intersects(app.frame)
            }
            let actualButtons = bars.flatMap { actualBar in
                actualBar.buttons.allElementsBoundByAccessibilityElement.map {
                    ["label": $0.label, "selected": String($0.isSelected),
                     "hittable": String($0.isHittable), "frame": String(describing: $0.frame)]
                }
            }
            let selected = actualButtons.filter { $0["selected"] == "true" && $0["hittable"] == "true" }
            let trace = app.descendants(matching: .any).matching(identifier: "hold-trace").firstMatch
            let traceText = trace.value as? String ?? "<unavailable>"
            let probe = app.descendants(matching: .any).matching(identifier: "native-controller-probe").firstMatch
            let actualLabel = app.staticTexts["selection"].label
            let hud = app.staticTexts["actual-held-target"].exists
            let record: [String: Any] = ["mode": mode, "step": step, "requested_item": target + 1,
                "observed_at": ISO8601DateFormatter().string(from: Date()),
                "journey_phase": step < 4 ? "warmup" : "window-target",
                "actual_page_uuid": app.staticTexts["state.\(target)"].label,
                "actual_page_draft": app.textFields["draft.\(target)"].value as? String ?? "<unavailable>",
                "actual_initial_page_uuid": identity,
                "actual_initial_page_draft": startingDraft,
                "did_reach_requested_page": reached, "actual_buttons": actualButtons,
                "visible_bar_count": bars.count, "actual_selection_label": actualLabel,
                "actual_hold_events": traceText,
                "native_controller_observations": probe.value as? String ?? "<unavailable>",
                "actual_hud_exists_after_release": hud]
            let data = try JSONSerialization.data(withJSONObject: record, options: [.prettyPrinted, .sortedKeys])
            let observation = XCTAttachment(data: data, uniformTypeIdentifier: "public.json")
            observation.name = "\(mode)-\(step)-actual-combined-state"
            observation.lifetime = .keepAlways
            add(observation)
            let tree = XCTAttachment(string: app.debugDescription)
            tree.name = "\(mode)-\(step)-actual-accessibility-tree"
            tree.lifetime = .keepAlways
            add(tree)
            let image = XCTAttachment(screenshot: app.screenshot())
            image.name = "\(mode)-\(step)-actual-screen"
            image.lifetime = .keepAlways
            add(image)
            if reached && bars.count == 1 && selected.count > 1 { reproduced = true }
            XCTAssertTrue(reached, "The requested page must actually be visible.")
            XCTAssertEqual(actualLabel, "Selected: Item \(target + 1)")
            XCTAssertEqual(bars.count, 1)
            XCTAssertEqual(selected.count, 1, "A native bar must expose exactly one hittable selected button.")
            XCTAssertEqual(selected.first?["label"], "Item \(target + 1)")
            XCTAssertFalse(hud)
            if let traceData = traceText.data(using: .utf8),
               let events = try? JSONSerialization.jsonObject(with: traceData) as? [[String: Any]] {
                XCTAssertTrue(events.contains { $0["phase"] as? String == "after-commit" })
                XCTAssertFalse(events.contains { [4, 5].contains($0["recognizer_state"] as? Int ?? -1) },
                    "Actual native gestures must end normally.")
            } else { XCTFail("The actual native hold trace must be valid JSON.") }
        }
        XCTAssertEqual(app.staticTexts["state.0"].label, identity)
        XCTAssertEqual(app.textFields["draft.0"].value as? String, "Generic unsent draft")
        return reproduced
    }
}
