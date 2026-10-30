import SwiftUI
import UIKit

private enum Variant: String, CaseIterable {
    case outerFit, candidateFit, combinedFit, horizontal, adaptive, labeled
}

private struct Experiment {
    let variant: Variant
    let decorated: Bool
    let symbolHint: Bool
    let accessibilitySize: Bool
    let token: String

    init() {
        let arguments = ProcessInfo.processInfo.arguments
        func value(_ key: String, _ fallback: String) -> String {
            guard let index = arguments.firstIndex(of: key), arguments.indices.contains(index + 1) else { return fallback }
            return arguments[index + 1]
        }
        variant = Variant(rawValue: value("--variant", "outerFit")) ?? .outerFit
        decorated = value("--decorated", "0") == "1"
        symbolHint = value("--symbol-hint", "1") == "1"
        accessibilitySize = value("--accessibility-size", "0") == "1"
        token = value("--token", "manual")
    }
}

@main
struct ListSizingApp: App {
    private let experiment = Experiment()
    var body: some Scene {
        WindowGroup {
            ExperimentScreen(experiment: experiment)
                .dynamicTypeSize(experiment.accessibilitySize ? .accessibility5 : .large)
                .preferredColorScheme(.dark)
        }
    }
}

private struct BoundsKey: PreferenceKey {
    static let defaultValue: [String: [CGRect]] = [:]
    static func reduce(value: inout [String: [CGRect]], nextValue: () -> [String: [CGRect]]) {
        value.merge(nextValue(), uniquingKeysWith: { previous, newer in
            newer.reduce(into: previous) { if !$0.contains($1) { $0.append($1) } }
        })
    }
}

private extension View {
    func measured(_ name: String) -> some View {
        background { GeometryReader { geometry in
            Color.clear.preference(key: BoundsKey.self, value: [name: [geometry.frame(in: .global)]])
        } }
    }
}

private struct DefaultDecoration: ViewModifier {
    let enabled: Bool
    @ViewBuilder func body(content: Content) -> some View {
        if enabled {
            content
                .frame(width: nil, height: nil)
                .frame(minWidth: nil, maxWidth: nil, minHeight: nil, maxHeight: nil)
                .padding(EdgeInsets())
                .background { EmptyView() }
                .overlay { EmptyView() }
                .shadow(color: .clear, radius: 0)
                .opacity(1)
        } else {
            content
        }
    }
}

private struct ExperimentScreen: View {
    let experiment: Experiment
    @Environment(\.horizontalSizeClass) private var horizontalSizeClass
    @Environment(\.verticalSizeClass) private var verticalSizeClass
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize

    var body: some View {
        NavigationSplitView {
            List(0..<10) { index in Text("Section \(index + 1)") }
                .navigationTitle("Examples")
                .navigationSplitViewColumnWidth(256)
        } detail: {
            VStack(spacing: 0) {
                HStack {
                    Text("List layout experiment").font(.headline)
                    Spacer()
                    Button("New item") {}
                }.padding(20)
                List {
                    Section("Compact reference") {
                        HStack(spacing: 12) {
                            Image(systemName: "circle.grid.2x2").frame(width: 34, height: 34)
                            VStack(alignment: .leading, spacing: 2) {
                                Text("Reference example").font(.body.weight(.medium))
                                Text("An ordinary row with text and progress.").font(.caption)
                            }
                            Spacer()
                            Text("0 of 12").font(.caption)
                            Image(systemName: "chevron.right").font(.caption)
                        }.padding(12).measured("reference")
                    }
                    Section {
                        ExperimentalCard(experiment: experiment)
                        Text("Following row").measured("following")
                    } header: {
                        VStack(alignment: .leading, spacing: 2) {
                            Text("Experiment: \(experiment.variant.rawValue)")
                            Text("Decoration \(experiment.decorated ? "on" : "off") · \(experiment.symbolHint ? "Label" : "Text") hint")
                        }
                    }
                }
                .listStyle(.plain)
                .scrollContentBackground(.hidden)
            }
            .navigationTitle("Sizing")
            .measured("detail")
        }
        .measured("window")
        .onPreferenceChange(BoundsKey.self) { bounds in
            Task { @MainActor in save(bounds) }
        }
    }

    private func save(_ bounds: [String: [CGRect]]) {
        // Fitting containers may inspect more than one candidate. Keep every
        // distinct real frame instead of overwriting duplicate inner keys.
        let frames = bounds.mapValues { values in
            values.map { [Double($0.minX), Double($0.minY), Double($0.width), Double($0.height)] }
        }
        let output: [String: Any] = [
            "schema_version": 1,
            "token": experiment.token,
            "variant": experiment.variant.rawValue,
            "decorated": experiment.decorated,
            "symbol_hint": experiment.symbolHint,
            "accessibility_size": experiment.accessibilitySize,
            "pid": ProcessInfo.processInfo.processIdentifier,
            "os_version": UIDevice.current.systemVersion,
            "device_model": UIDevice.current.model,
            "horizontal_size_class": horizontalSizeClass == .regular ? "regular" : "compact",
            "vertical_size_class": verticalSizeClass == .regular ? "regular" : "compact",
            "dynamic_type": String(describing: dynamicTypeSize),
            "frames": frames
        ]
        guard let data = try? JSONSerialization.data(withJSONObject: output, options: [.sortedKeys, .prettyPrinted]),
              let directory = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask).first else { return }
        try? data.write(to: directory.appendingPathComponent("measurement.json"), options: .atomic)
    }
}

private struct ExperimentalCard: View {
    let experiment: Experiment
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize
    private var decoration: DefaultDecoration { DefaultDecoration(enabled: experiment.decorated) }

    var body: some View {
        Button {} label: {
            Group {
                switch experiment.variant {
                case .outerFit:
                    ViewThatFits(in: .horizontal) {
                        horizontalSummary(constrainHeight: false).measured("fit_wide")
                        stackedSummary.measured("fit_stacked")
                    }.fixedSize(horizontal: false, vertical: true)
                case .candidateFit:
                    ViewThatFits(in: .horizontal) {
                        horizontalSummary(constrainHeight: true).fixedSize(horizontal: false, vertical: true).measured("fit_wide")
                        stackedSummary.fixedSize(horizontal: false, vertical: true).measured("fit_stacked")
                    }
                case .combinedFit:
                    ViewThatFits(in: .horizontal) {
                        horizontalSummary(constrainHeight: true).fixedSize(horizontal: false, vertical: true).measured("fit_wide")
                        stackedSummary.fixedSize(horizontal: false, vertical: true).measured("fit_stacked")
                    }.fixedSize(horizontal: false, vertical: true)
                case .horizontal:
                    HStack(alignment: .top, spacing: 12) {
                        marker.fixedSize()
                        summary.fixedSize(horizontal: false, vertical: true).layoutPriority(1)
                        spacer
                        progress.fixedSize()
                        chevron.fixedSize()
                    }
                case .adaptive:
                    let layout = dynamicTypeSize.isAccessibilitySize
                        ? AnyLayout(VStackLayout(alignment: .leading, spacing: 12))
                        : AnyLayout(HStackLayout(alignment: .top, spacing: 12))
                    layout {
                        marker.fixedSize()
                        summary.fixedSize(horizontal: false, vertical: true).layoutPriority(1)
                        progress.fixedSize()
                        chevron.fixedSize()
                    }
                case .labeled:
                    HStack(alignment: .top, spacing: 12) {
                        marker.fixedSize()
                        LabeledContent {
                            progress.fixedSize()
                        } label: {
                            summary.fixedSize(horizontal: false, vertical: true)
                        }
                        chevron.fixedSize()
                    }
                }
            }
            .padding(12)
            .contentShape(.rect)
            .modifier(decoration)
            .measured("card_content")
        }
        .buttonStyle(.plain)
        .background(.quaternary, in: RoundedRectangle(cornerRadius: 16))
        .overlay(RoundedRectangle(cornerRadius: 16).stroke(.secondary, lineWidth: 1))
        .modifier(decoration)
        .measured("card")
        .listRowSeparator(.hidden)
        .listRowBackground(Color.clear)
    }

    private func horizontalSummary(constrainHeight: Bool) -> some View {
        HStack(spacing: 12) {
            marker.fixedSize()
            summary.fixedSize(horizontal: true, vertical: constrainHeight)
            spacer
            progress.fixedSize()
            chevron.fixedSize()
        }
    }

    private var stackedSummary: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .top, spacing: 12) {
                marker.fixedSize()
                summary.fixedSize(horizontal: false, vertical: true)
                    .frame(maxWidth: .infinity, alignment: .leading).layoutPriority(1)
                chevron.fixedSize()
            }
            HStack { spacer; progress }
        }
    }

    private var marker: some View {
        ZStack {
            Circle().stroke(.secondary, lineWidth: 3).frame(width: 34, height: 34).modifier(decoration)
            Text("1").font(.callout.weight(.bold)).modifier(decoration)
        }.modifier(decoration).measured("marker")
    }

    private var summary: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text("How does this example work?")
                .font(.body.weight(.medium)).modifier(decoration).measured("title")
            Text("Open one sample and inspect its ordinary content.")
                .font(.caption).foregroundStyle(.secondary).modifier(decoration)
            Group {
                if experiment.symbolHint {
                    Label("This example requires a small optional sample file. Download it before continuing; it is less than ten megabytes.", systemImage: "arrow.down.circle")
                } else {
                    Text("This example requires a small optional sample file. Download it before continuing; it is less than ten megabytes.")
                }
            }.font(.caption2).foregroundStyle(.orange).modifier(decoration).measured("hint")
        }.modifier(decoration).measured("summary")
    }

    private var spacer: some View { Spacer().modifier(decoration) }
    private var progress: some View {
        VStack(alignment: .trailing, spacing: 2) {
            Text("0/8").font(.caption.monospacedDigit()).modifier(decoration)
            Text("complete").font(.caption2).foregroundStyle(.secondary).modifier(decoration)
        }.modifier(decoration).measured("progress")
    }
    private var chevron: some View {
        Image(systemName: "chevron.right").font(.caption).foregroundStyle(.secondary).modifier(decoration)
    }
}
