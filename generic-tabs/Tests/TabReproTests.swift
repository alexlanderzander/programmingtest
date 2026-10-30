import XCTest
import UIKit

@MainActor
final class TabReproTests: XCTestCase {
    func testCombinedNativeHoldWindow() throws {
        continueAfterFailure = true
        let reproduced = try exerciseCombined("hold-combined")
        let decision = XCTAttachment(string: reproduced
            ? "The State-owned window repair probe still observed multiple selected native tabs. Preserve the finalized failure evidence."
            : "The State-owned window repair probe observed no duplicated selection. Finalized route, state and saved-window assertions remain authoritative; actual Atlas validation is pending.")
        decision.name = "state-owned-window-candidate-observation"
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
        let initialBar = app.tabBars.firstMatch
        XCTAssertTrue(initialBar.waitForExistence(timeout: 10) && initialBar.isHittable)
        let initialBars = app.tabBars.allElementsBoundByAccessibilityElement.filter {
            $0.exists && $0.isHittable && !$0.frame.isEmpty && $0.frame.intersects(app.frame)
        }
        let initialButtons = initialBars.flatMap { bar in
            bar.buttons.allElementsBoundByAccessibilityElement.map {
                ["label": $0.label, "selected": String($0.isSelected), "hittable": String($0.isHittable)]
            }
        }
        let initialVisibleLabels = initialButtons.filter { $0["hittable"] == "true" }.map { $0["label"] ?? "" }
        let initialSelectedLabels = initialButtons.filter {
            $0["hittable"] == "true" && $0["selected"] == "true"
        }.map { $0["label"] ?? "" }
        let initialRecord: [String: Any] = ["stage": "initial-fixture",
            "visible_native_bar_count": initialBars.count,
            "actual_buttons": initialButtons, "actual_visible_labels": initialVisibleLabels,
            "actual_selected_labels": initialSelectedLabels,
            "actual_page_label": app.staticTexts["selection"].label,
            "storage_value_directly_read": false]
        let initialData = try JSONSerialization.data(withJSONObject: initialRecord, options: [.prettyPrinted, .sortedKeys])
        let initialAttachment = XCTAttachment(data: initialData, uniformTypeIdentifier: "public.json")
        initialAttachment.name = "actual-initial-native-tab-window"
        initialAttachment.lifetime = .keepAlways
        add(initialAttachment)
        XCTAssertEqual(initialBars.count, 1)
        XCTAssertEqual(initialVisibleLabels, (1...5).map { "Item \($0)" })
        XCTAssertEqual(initialSelectedLabels, ["Item 1"])
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

        func actualNativeProbe() throws -> [[String: Any]] {
            let probe = app.descendants(matching: .any).matching(identifier: "native-controller-probe").firstMatch
            let ready = XCTNSPredicateExpectation(
                predicate: NSPredicate(format: "exists == true AND value CONTAINS %@", "\"process_id\""), object: probe)
            XCTAssertEqual(XCTWaiter.wait(for: [ready], timeout: min(10, max(0.1, deadline.timeIntervalSinceNow))), .completed)
            let text = try XCTUnwrap(probe.value as? String)
            let data = try XCTUnwrap(text.data(using: .utf8))
            return try XCTUnwrap(try JSONSerialization.jsonObject(with: data) as? [[String: Any]])
        }
        let beforeNative = try actualNativeProbe()
        XCTAssertEqual(beforeNative.count, 1)
        let before = try XCTUnwrap(beforeNative.first)
        let savedWindow = try XCTUnwrap(before["saved_window_value"] as? Int)
        let observedWindow = try XCTUnwrap(before["view_owned_window_start"] as? Int)
        let beforePID = try XCTUnwrap(before["process_id"] as? Int)
        let beforeDraft = app.textFields["draft.0"].value as? String ?? "<unavailable>"
        XCTAssertEqual(savedWindow, observedWindow)
        XCTAssertTrue((0...9).contains(savedWindow))
        guard (0...9).contains(savedWindow), Date() < deadline else {
            XCTFail("The saved native window or bounded relaunch precondition is unavailable.")
            return reproduced
        }
        let expectedLabels = ["Item 1"] + ((savedWindow + 2)...(savedWindow + 5)).map { "Item \($0)" }
        app.terminate()
        app.launch()
        XCTAssertTrue(app.staticTexts["page.0"].waitForExistence(timeout: min(30, max(0.1, deadline.timeIntervalSinceNow))))
        let relaunchedBar = app.tabBars.firstMatch
        XCTAssertTrue(relaunchedBar.waitForExistence(timeout: min(10, max(0.1, deadline.timeIntervalSinceNow))) && relaunchedBar.isHittable)
        let afterNative = try actualNativeProbe()
        XCTAssertEqual(afterNative.count, 1)
        let after = try XCTUnwrap(afterNative.first)
        let afterPID = try XCTUnwrap(after["process_id"] as? Int)
        let newIdentity = app.staticTexts["state.0"].label
        let restoredBars = app.tabBars.allElementsBoundByAccessibilityElement.filter {
            $0.exists && $0.isHittable && !$0.frame.isEmpty && $0.frame.intersects(app.frame)
        }
        let restoredButtons = restoredBars.flatMap { bar in
            bar.buttons.allElementsBoundByAccessibilityElement.map {
                ["label": $0.label, "selected": String($0.isSelected), "hittable": String($0.isHittable)]
            }
        }
        let restoredLabels = restoredButtons.filter { $0["hittable"] == "true" }.map { $0["label"] ?? "" }
        let restoredSelected = restoredButtons.filter {
            $0["hittable"] == "true" && $0["selected"] == "true"
        }.map { $0["label"] ?? "" }
        let restoration: [String: Any] = ["stage": "saved-window-relaunch",
            "before_native_state": beforeNative, "after_native_state": afterNative,
            "before_page_uuid": identity, "after_page_uuid": newIdentity,
            "before_draft": beforeDraft,
            "after_draft": app.textFields["draft.0"].value as? String ?? "<unavailable>",
            "actual_buttons": restoredButtons, "actual_visible_labels": restoredLabels,
            "actual_selected_labels": restoredSelected,
            "expected_titles_from_measured_saved_window": expectedLabels,
            "saved_window_directly_observed_in_app_process": true]
        let restoreData = try JSONSerialization.data(withJSONObject: restoration, options: [.prettyPrinted, .sortedKeys])
        let restoreRecord = XCTAttachment(data: restoreData, uniformTypeIdentifier: "public.json")
        restoreRecord.name = "actual-saved-window-relaunch"
        restoreRecord.lifetime = .keepAlways
        add(restoreRecord)
        let restoreTree = XCTAttachment(string: app.debugDescription)
        restoreTree.name = "actual-saved-window-relaunch-accessibility-tree"
        restoreTree.lifetime = .keepAlways
        add(restoreTree)
        let restoreImage = XCTAttachment(screenshot: app.screenshot())
        restoreImage.name = "actual-saved-window-relaunch-screen"
        restoreImage.lifetime = .keepAlways
        add(restoreImage)
        XCTAssertEqual(restoredBars.count, 1)
        XCTAssertEqual(restoredLabels, expectedLabels)
        XCTAssertEqual(restoredSelected, ["Item 1"])
        XCTAssertEqual(after["saved_window_value"] as? Int, savedWindow)
        XCTAssertEqual(after["view_owned_window_start"] as? Int, savedWindow)
        XCTAssertNotEqual(afterPID, beforePID)
        XCTAssertNotEqual(newIdentity, identity)
        XCTAssertNotNil(UUID(uuidString: newIdentity))
        XCTAssertGreaterThan(deadline.timeIntervalSinceNow, 0, "The bounded saved-window relaunch check expired.")
        return reproduced
    }
}
