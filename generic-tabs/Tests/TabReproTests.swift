import XCTest
import UIKit

@MainActor
final class TabReproTests: XCTestCase {
    func testBaseline() throws { try exercise("baseline") }
    func testLabelIdentity() throws { try exercise("label-id") }
    func testNativeItemTraits() throws { try exercise("native-traits") }
    func testVariedSymbols() throws { try exercise("varied-symbols") }
    func testSplitColumns() throws { try exercise("split-columns") }
    func testVisitedGating() throws { try exercise("visited-gating") }
    func testBindingGuard() throws { try exercise("binding-guard") }
    func testHoldOnly() throws { try exercise("hold-only") }

    func testHoldDisableTrace() throws { try traceHold("hold-disable") }
    func testHoldKeepEnabledTrace() throws { try traceHold("hold-keep-enabled") }

    private func traceHold(_ mode: String) throws {
        continueAfterFailure = true
        let app = XCUIApplication()
        app.launchEnvironment["REPRO_MODE"] = mode
        app.launch()
        XCTAssertTrue(app.staticTexts["page.0"].waitForExistence(timeout: 30))
        let identity = app.staticTexts["state.0"].label
        app.textFields["draft.0"].tap()
        app.textFields["draft.0"].typeText("Generic unsent draft")
        app.buttons["hide-keyboard"].tap()
        for (step, target) in [4, 5, 6, 0].enumerated() {
            let bar = try XCTUnwrap(app.tabBars.allElementsBoundByAccessibilityElement.first(where: { $0.isHittable }))
            let y = (bar.frame.midY - app.frame.minY) / app.frame.height
            app.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: y))
                .press(forDuration: 0.7, thenDragTo: app.coordinate(withNormalizedOffset:
                    CGVector(dx: (CGFloat(target) + 0.5) / 14, dy: y)),
                    withVelocity: .slow, thenHoldForDuration: 0.4)
            let didReach = app.staticTexts["page.\(target)"].waitForExistence(timeout: 3)
            let actualButtons = bar.buttons.allElementsBoundByAccessibilityElement.map {
                ["label": $0.label, "selected": String($0.isSelected), "hittable": String($0.isHittable)]
            }
            let trace = app.descendants(matching: .any).matching(identifier: "hold-trace").firstMatch
            let probe = app.descendants(matching: .any).matching(identifier: "native-controller-probe").firstMatch
            let observation: [String: Any] = ["mode": mode, "step": step, "requested_item": target + 1,
                "did_reach_requested_page": didReach, "actual_buttons": actualButtons,
                "actual_selection_label": app.staticTexts["selection"].label,
                "actual_hold_events": trace.value as? String ?? "<unavailable>",
                "native_controller_observations": probe.value as? String ?? "<unavailable>",
                "actual_hud_exists_after_release": app.staticTexts["actual-held-target"].exists]
            let data = try JSONSerialization.data(withJSONObject: observation, options: [.prettyPrinted, .sortedKeys])
            let record = XCTAttachment(data: data, uniformTypeIdentifier: "public.json")
            record.name = "\(mode)-\(step)-actual-hold-trace"; record.lifetime = .keepAlways; add(record)
            let image = XCTAttachment(screenshot: app.screenshot())
            image.name = "\(mode)-\(step)-actual-screen"; image.lifetime = .keepAlways; add(image)
            XCTAssertTrue(didReach)
            let selected = actualButtons.filter { $0["selected"] == "true" && $0["hittable"] == "true" }
            XCTAssertEqual(selected.count, 1)
            XCTAssertEqual(selected.first?["label"], "Item \(target + 1)")
            XCTAssertFalse(app.staticTexts["actual-held-target"].exists)
        }
        XCTAssertEqual(app.staticTexts["state.0"].label, identity)
        XCTAssertEqual(app.textFields["draft.0"].value as? String, "Generic unsent draft")
    }

    private func exercise(_ mode: String) throws {
        continueAfterFailure = true
        let app = XCUIApplication()
        app.launchEnvironment["REPRO_MODE"] = mode
        app.launch()
        XCTAssertTrue(app.staticTexts["page.0"].waitForExistence(timeout: 30))
        let firstIdentity = app.staticTexts["state.0"].label
        app.textFields["draft.0"].tap()
        app.textFields["draft.0"].typeText("Generic unsent draft")
        app.buttons["hide-keyboard"].tap()
        for step in 0...14 {
            let index = step % 14
            let page = app.staticTexts["page.\(index)"]
            XCTAssertTrue(page.waitForExistence(timeout: 8))
            XCTAssertEqual(app.staticTexts["selection"].label, "Selected: Item \(index + 1)")
            let bars = app.tabBars.allElementsBoundByAccessibilityElement.filter {
                $0.exists && !$0.frame.isEmpty && $0.frame.intersects(app.frame) && $0.isHittable
            }
            let snapshots = bars.map { bar in
                bar.buttons.allElementsBoundByAccessibilityElement.map { button in
                    ["label": button.label, "selected": String(button.isSelected),
                     "hittable": String(button.isHittable), "frame": String(describing: button.frame)]
                }
            }
            let selected = snapshots.flatMap { $0 }.filter { $0["selected"] == "true" && $0["hittable"] == "true" }
            let probe = app.descendants(matching: .any).matching(identifier: "native-controller-probe").firstMatch
            let observation: [String: Any] = ["mode": mode, "step": step, "expected_item": index + 1,
                "visible_bar_count": bars.count, "actual_buttons": snapshots,
                "native_controller_observations": probe.value as? String ?? "<unavailable>"]
            let data = try JSONSerialization.data(withJSONObject: observation, options: [.prettyPrinted, .sortedKeys])
            let record = XCTAttachment(data: data, uniformTypeIdentifier: "public.json")
            record.name = "\(mode)-\(step)-actual-observations"
            record.lifetime = .keepAlways
            add(record)
            let png = XCTAttachment(screenshot: app.screenshot())
            png.name = "\(mode)-\(step)-actual-screen"
            png.lifetime = .keepAlways
            add(png)
            if [0, 4, 5, 6, 9, 11, 14].contains(step) {
                let tree = XCTAttachment(string: app.debugDescription)
                tree.name = "\(mode)-\(step)-actual-accessibility-tree"
                tree.lifetime = .keepAlways
                add(tree)
            }
            XCTAssertEqual(bars.count, 1)
            XCTAssertEqual(selected.count, 1, "\(mode) step\(step): native AX must have exactly one selected item")
            XCTAssertEqual(selected.first?["label"], "Item \(index + 1)")
            if step < 14 {
                if mode == "hold-only", let bar = bars.first {
                    let y = (bar.frame.midY - app.frame.minY) / app.frame.height
                    let destination = (step + 1) % 14
                    app.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: y))
                        .press(forDuration: 0.7, thenDragTo: app.coordinate(withNormalizedOffset:
                            CGVector(dx: (CGFloat(destination) + 0.5) / 14, dy: y)),
                            withVelocity: .slow, thenHoldForDuration: 0.4)
                } else { app.buttons["next"].tap() }
            }
        }
        XCTAssertEqual(app.staticTexts["state.0"].label, firstIdentity)
        XCTAssertEqual(app.textFields["draft.0"].value as? String, "Generic unsent draft")
    }
}
