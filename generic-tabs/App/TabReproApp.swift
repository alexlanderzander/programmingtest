import SwiftUI
import UIKit

@main
struct TabReproApp: App {
    var body: some Scene {
        WindowGroup {
            let mode = ProcessInfo.processInfo.environment["REPRO_MODE"] ?? "baseline"
            if mode.hasPrefix("anchor-") { NativePreferenceProbe(mode: mode) }
            else { TabRepro(mode: mode) }
        }
    }
}

struct TabRepro: View {
    let mode: String
    @State private var selected = 0
    @State private var windowStart = 0
    @State private var columns: NavigationSplitViewVisibility = .detailOnly
    @State private var compactColumn: NavigationSplitViewColumn = .detail
    @State private var visited: Set<Int> = [0]
    @State private var pendingSelection: Int?
    @State private var pendingTask: Task<Void, Never>?
    @State private var heldIndex: Int?

    private var combined: Bool { mode.hasPrefix("hold-combined") }
    private var atomic: Bool { mode == "hold-combined-atomic" }

    private var visible: [Int] { [0] + Array((windowStart + 1)...(windowStart + 4)) }
    private var selection: Binding<Int> {
        if mode == "binding-guard" || mode == "hold-guard" || combined {
            Binding(get: { selected }, set: { value in
                guard pendingSelection == nil || pendingSelection == value else { return }
                move(to: value)
            })
        } else { $selected }
    }

    private func move(to next: Int) {
        if mode == "visited-gating" || (combined && !atomic) { visited.insert(next) }
        if combined && !atomic { columns = .detailOnly; compactColumn = .detail }
        var start = windowStart
        if next > 0 {
            if next - 1 < start { start = next - 1 }
            else if next - 1 >= start + 4 { start = next - 4 }
        }
        var transaction = Transaction(animation: nil)
        transaction.disablesAnimations = true
        withTransaction(transaction) {
            if atomic {
                visited.insert(next)
                columns = .detailOnly
                compactColumn = .detail
                heldIndex = nil
            }
            windowStart = start
            if mode == "binding-guard" || mode == "hold-guard" || combined {
                pendingSelection = next
                pendingTask?.cancel()
                // This mode reproduces a suspected callback guard, not a
                // proposed delay-based repair.
                pendingTask = Task { @MainActor in
                    try? await Task.sleep(for: .milliseconds(400))
                    guard !Task.isCancelled else { return }
                    pendingSelection = nil
                }
            }
            selected = next
        }
    }
    private func symbol(_ index: Int) -> String {
        let symbols = ["star", "heart", "pencil", "paperclip", "bell", "bolt", "moon", "sun.max",
                       "folder", "book", "leaf", "globe", "link", "tray"]
        return mode == "varied-symbols" ? symbols[index] : "circle"
    }

    @ViewBuilder private func page(_ index: Int) -> some View {
        if combined {
            if visited.contains(index) {
                NavigationSplitView(columnVisibility: $columns, preferredCompactColumn: $compactColumn) {
                    List { Text("Generic sidebar") }
                } detail: {
                    NavigationStack { ReproPage(index: index) }
                        .accessibilityElement(children: .contain)
                        .accessibilityHidden(index != selected)
                        .toolbarVisibility(.visible, for: .tabBar)
                }
                .navigationSplitViewStyle(.balanced)
                .toolbar(removing: .sidebarToggle)
            }
        } else if mode == "visited-gating" {
            if visited.contains(index) { ReproPage(index: index) }
        } else if mode == "split-columns" {
            NavigationSplitView(columnVisibility: $columns, preferredCompactColumn: $compactColumn) {
                List { Text("Generic sidebar") }
            } detail: {
                NavigationStack { ReproPage(index: index) }
            }
            .navigationSplitViewStyle(.balanced)
            .toolbar(removing: .sidebarToggle)
        } else {
            ReproPage(index: index)
        }
    }

    var body: some View {
        VStack {
            Text("Mode: \(mode)")
            Text("Selected: Item \(selected + 1)").accessibilityIdentifier("selection")
            HStack {
                Button("Next") {
                    move(to: (selected + 1) % 14)
                }.accessibilityIdentifier("next")
                Button("Hide keyboard") {
                    UIApplication.shared.sendAction(#selector(UIResponder.resignFirstResponder), to: nil, from: nil, for: nil)
                }.accessibilityIdentifier("hide-keyboard")
            }
            TabView(selection: selection) {
                ForEach(0..<14) { index in
                    if mode == "label-id" {
                        Tab(value: index) { page(index) } label: {
                            Label("Item \(index + 1)", systemImage: symbol(index))
                                .id(windowStart)
                        }
                        .hidden(!visible.contains(index))
                    } else {
                        Tab("Item \(index + 1)", systemImage: symbol(index), value: index) {
                            page(index)
                        }
                        .hidden(!visible.contains(index))
                    }
                }
            }
        }
        .overlay(alignment: .topLeading) {
            NativeControllerProbe(mode: mode, generation: selected)
                .frame(width: 1, height: 1)
        }
        .overlay(alignment: .top) {
            if let heldIndex {
                Text("Holding Item \(heldIndex + 1)")
                    .padding().background(.regularMaterial, in: Capsule())
                    .accessibilityIdentifier("actual-held-target")
                    .transition(.move(edge: .top).combined(with: .opacity))
            }
        }
        .background {
            GenericNativeHold(enabled: mode.hasPrefix("hold-"), disableBar: mode != "hold-keep-enabled",
                              heldChanged: { next in
                                  if combined {
                                      guard heldIndex != next else { return }
                                      if next == nil { withAnimation(.easeOut(duration: 0.2)) { heldIndex = nil } }
                                      else if heldIndex == nil { withAnimation(.spring(duration: 0.3)) { heldIndex = next } }
                                      else { heldIndex = next }
                                  } else { heldIndex = next }
                              }, commit: move)
                .frame(width: 1, height: 1)
        }
    }
}

struct GenericNativeHold: UIViewRepresentable {
    let enabled: Bool
    let disableBar: Bool
    let heldChanged: (Int?) -> Void
    let commit: (Int) -> Void
    func makeCoordinator() -> Coordinator { Coordinator() }
    func makeUIView(context: Context) -> HoldView {
        let view = HoldView()
        view.owner = context.coordinator
        view.isUserInteractionEnabled = false
        view.isAccessibilityElement = true
        view.accessibilityIdentifier = "hold-trace"
        view.accessibilityLabel = "Actual native hold events"
        context.coordinator.traceView = view
        return view
    }
    func updateUIView(_ view: HoldView, context: Context) {
        context.coordinator.enabled = enabled
        context.coordinator.disableBar = disableBar
        context.coordinator.heldChanged = heldChanged
        context.coordinator.commit = commit
        context.coordinator.attach(to: view.window)
    }
    static func dismantleUIView(_ view: HoldView, coordinator: Coordinator) { coordinator.attach(to: nil) }

    final class HoldView: UIView {
        weak var owner: Coordinator?
        override func didMoveToWindow() { super.didMoveToWindow(); owner?.attach(to: window) }
    }

    @MainActor final class Coordinator: NSObject, UIGestureRecognizerDelegate {
        var enabled = false
        var disableBar = true
        var heldChanged: (Int?) -> Void = { _ in }
        weak var traceView: HoldView?
        private var events: [[String: Any]] = []
        var commit: (Int) -> Void = { _ in }
        private weak var window: UIWindow?
        private weak var heldBar: UITabBar?
        private var priorInteraction = true
        private var recognizer: UILongPressGestureRecognizer?
        private var lastIndex = 0
        private var holding = false

        private func log(_ phase: String, recognizer: UIGestureRecognizer? = nil) {
            let point = recognizer?.location(in: window) ?? .zero
            events.append(["phase": phase, "uptime": ProcessInfo.processInfo.systemUptime,
                "recognizer_state": recognizer?.state.rawValue ?? -1,
                "x": point.x, "y": point.y, "last_index": lastIndex, "holding": holding,
                "bar_interaction_enabled": heldBar?.isUserInteractionEnabled as Any? ?? NSNull(),
                "actual_bar_selected_title": heldBar?.selectedItem?.title ?? "<nil>"])
            if events.count > 180 { events.removeFirst(events.count - 180) }
            if let data = try? JSONSerialization.data(withJSONObject: events, options: [.sortedKeys]),
               let value = String(data: data, encoding: .utf8) { traceView?.accessibilityValue = value }
        }

        func attach(to target: UIWindow?) {
            if window === target && (recognizer != nil || !enabled) { return }
            heldBar?.isUserInteractionEnabled = priorInteraction
            if let recognizer { window?.removeGestureRecognizer(recognizer) }
            recognizer = nil
            window = target
            guard enabled, let target else { return }
            let press = UILongPressGestureRecognizer(target: self, action: #selector(hold(_:)))
            press.minimumPressDuration = 0.3
            press.allowableMovement = 80
            press.cancelsTouchesInView = true
            press.delegate = self
            target.addGestureRecognizer(press)
            recognizer = press
        }

        private func bar(at point: CGPoint, in window: UIWindow) -> UITabBar? {
            guard let root = window.rootViewController else { return nil }
            var pending = [root]
            while let controller = pending.popLast() {
                pending += controller.children
                if let tabs = controller as? UITabBarController {
                    let bar = tabs.tabBar
                    if !bar.isHidden && bar.window === window && bar.convert(bar.bounds, to: window).contains(point) { return bar }
                }
            }
            return nil
        }
        func gestureRecognizerShouldBegin(_ recognizer: UIGestureRecognizer) -> Bool {
            guard enabled, let window else { return false }
            heldBar = bar(at: recognizer.location(in: window), in: window)
            log("should-begin", recognizer: recognizer)
            return heldBar != nil
        }
        func gestureRecognizer(_ recognizer: UIGestureRecognizer, shouldRecognizeSimultaneouslyWith other: UIGestureRecognizer) -> Bool { true }
        @objc private func hold(_ recognizer: UILongPressGestureRecognizer) {
            guard let window, window.bounds.width > 0 else { return }
            func index() -> Int { min(13, max(0, Int(recognizer.location(in: window).x / window.bounds.width * 14))) }
            log("callback-enter", recognizer: recognizer)
            switch recognizer.state {
            case .began:
                holding = true
                priorInteraction = heldBar?.isUserInteractionEnabled ?? true
                log("before-disable", recognizer: recognizer)
                if disableBar { heldBar?.isUserInteractionEnabled = false }
                log("after-disable", recognizer: recognizer)
                lastIndex = index()
                heldChanged(lastIndex)
                log("began-finished", recognizer: recognizer)
            case .changed:
                if holding { lastIndex = index(); heldChanged(lastIndex) }
                log("changed-finished", recognizer: recognizer)
            case .ended, .cancelled, .failed:
                guard holding else { log("end-without-holding", recognizer: recognizer); return }
                holding = false
                log("before-restore", recognizer: recognizer)
                heldBar?.isUserInteractionEnabled = priorInteraction
                log("after-restore", recognizer: recognizer)
                log("before-commit", recognizer: recognizer)
                commit(lastIndex)
                log("after-commit", recognizer: recognizer)
                heldChanged(nil)
                heldBar = nil
            default: break
            }
        }
    }
}

struct ReproPage: View {
    let index: Int
    @State private var draft = ""
    @State private var identity = UUID().uuidString

    var body: some View {
        VStack(spacing: 16) {
            Text("Page \(index + 1)").accessibilityIdentifier("page.\(index)")
            Text(identity).font(.caption).accessibilityIdentifier("state.\(index)")
            TextField("Dummy draft", text: $draft).textFieldStyle(.roundedBorder)
                .accessibilityIdentifier("draft.\(index)")
        }.padding()
    }
}

struct NativeControllerProbe: UIViewRepresentable {
    let mode: String
    let generation: Int
    func makeUIView(context: Context) -> ProbeView { ProbeView() }
    func updateUIView(_ view: ProbeView, context: Context) {
        view.mode = mode
        view.setNeedsLayout()
    }

    final class ProbeView: UIView {
        var mode = "baseline"
        override init(frame: CGRect) {
            super.init(frame: frame)
            isUserInteractionEnabled = false
            isAccessibilityElement = true
            accessibilityIdentifier = "native-controller-probe"
            accessibilityLabel = "Actual native controller observations"
        }
        required init?(coder: NSCoder) { return nil }
        override func didMoveToWindow() { super.didMoveToWindow(); setNeedsLayout() }
        override func layoutSubviews() {
            super.layoutSubviews()
            guard let root = window?.rootViewController else { return }
            var pending = [root]
            var observations: [[String: Any]] = []
            while let controller = pending.popLast() {
                pending += controller.children
                if let presented = controller.presentedViewController { pending.append(presented) }
                guard let tabs = controller as? UITabBarController else { continue }
                let bar = tabs.tabBar
                let selected = bar.selectedItem
                let items = bar.items ?? []
                let before = items.map { $0.accessibilityTraits.rawValue }
                observations.append([
                    "observed_uptime": ProcessInfo.processInfo.systemUptime,
                    "controller_selected_index": tabs.selectedIndex,
                    "selected_item_identity": selected.map { String(describing: ObjectIdentifier($0)) } ?? "<nil>",
                    "item_identities": items.map { String(describing: ObjectIdentifier($0)) },
                    "bar_selected_title": selected?.title ?? "<nil>",
                    "item_titles": items.map { $0.title ?? "<nil>" },
                    "item_traits_before": before,
                    "item_traits_after": items.map { $0.accessibilityTraits.rawValue },
                    "bar_hidden": bar.isHidden,
                    "bar_frame": String(describing: bar.frame),
                    "mode": mode,
                    "screen_native_bounds_pixels": String(describing: window?.windowScene?.screen.nativeBounds ?? .zero),
                    "screen_native_scale": window?.windowScene?.screen.nativeScale ?? 0,
                    "screen_render_scale": window?.windowScene?.screen.scale ?? 0,
                    "actual_interface_orientation": window?.windowScene?.effectiveGeometry.interfaceOrientation.rawValue ?? 0,
                ])
            }
            if let data = try? JSONSerialization.data(withJSONObject: observations, options: [.sortedKeys]),
               let text = String(data: data, encoding: .utf8) { accessibilityValue = text }
        }
    }
}


private struct GenericLabelsKey: PreferenceKey {
    static var defaultValue: [String] { [] }
    static func reduce(value: inout [String], nextValue: () -> [String]) {
        value.append(contentsOf: nextValue())
    }
}

/// Independent generic test of the public SwiftUI preference primitives.
private struct NativePreferenceProbe: View {
    let mode: String
    @State private var observed: [String] = []
    private var leaf: some View {
        Text("Item 1").padding()
            .anchorPreference(key: GenericLabelsKey.self, value: .bounds) { _ in ["Item 1"] }
    }
    @ViewBuilder private var nested: some View {
        if mode == "anchor-writer" {
            VStack { leaf }
                .anchorPreference(key: GenericLabelsKey.self, value: .bounds) { _ in ["Item 2"] }
        } else {
            VStack { leaf }
                .transformAnchorPreference(key: GenericLabelsKey.self, value: .bounds) { values, _ in
                    values.append("Item 2")
                }
        }
    }
    var body: some View {
        nested
            .onPreferenceChange(GenericLabelsKey.self) { observed = $0 }
            .overlay(alignment: .bottom) {
                Text(observed.joined(separator: ","))
                    .accessibilityIdentifier("actual-native-preferences")
            }
    }
}
