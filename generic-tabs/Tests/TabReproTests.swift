import XCTest
import UIKit

@MainActor
final class TabReproTests: XCTestCase {
    func testBaseline() throws { try exercise("baseline") }
    func testLabelIdentity() throws { try exercise("label-id") }
    func testNativeItemTraits() throws { try exercise("native-traits") }

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
            if step < 14 { app.buttons["next"].tap() }
        }
        XCTAssertEqual(app.staticTexts["state.0"].label, firstIdentity)
        XCTAssertEqual(app.textFields["draft.0"].value as? String, "Generic unsent draft")
    }
}
