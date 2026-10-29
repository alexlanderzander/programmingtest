import SwiftUI
import UIKit

@main
struct TabReproApp: App {
    var body: some Scene {
        WindowGroup {
            TabRepro(mode: ProcessInfo.processInfo.environment["REPRO_MODE"] ?? "baseline")
        }
    }
}

struct TabRepro: View {
    let mode: String
    @State private var selected = 0
    @State private var windowStart = 0
    @State private var columns: NavigationSplitViewVisibility = .detailOnly
    @State private var compactColumn: NavigationSplitViewColumn = .detail

    private var visible: [Int] { [0] + Array((windowStart + 1)...(windowStart + 4)) }
    private func symbol(_ index: Int) -> String {
        let symbols = ["star", "heart", "pencil", "paperclip", "bell", "bolt", "moon", "sun.max",
                       "folder", "book", "leaf", "globe", "link", "tray"]
        return mode == "varied-symbols" ? symbols[index] : "circle"
    }

    @ViewBuilder private func page(_ index: Int) -> some View {
        if mode == "split-columns" {
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
                    let next = (selected + 1) % 14
                    var start = windowStart
                    if next > 0 {
                        if next - 1 < start { start = next - 1 }
                        else if next - 1 >= start + 4 { start = next - 4 }
                    }
                    var transaction = Transaction(animation: nil)
                    transaction.disablesAnimations = true
                    withTransaction(transaction) {
                        windowStart = start
                        selected = next
                    }
                }.accessibilityIdentifier("next")
                Button("Hide keyboard") {
                    UIApplication.shared.sendAction(#selector(UIResponder.resignFirstResponder), to: nil, from: nil, for: nil)
                }.accessibilityIdentifier("hide-keyboard")
            }
            TabView(selection: $selected) {
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
                if mode == "native-traits", let selected {
                    for item in items {
                        if item === selected { item.accessibilityTraits.insert(.selected) }
                        else { item.accessibilityTraits.remove(.selected) }
                    }
                }
                observations.append([
                    "controller_selected_index": tabs.selectedIndex,
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
