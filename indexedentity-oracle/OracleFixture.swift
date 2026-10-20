import AppIntents
import CoreSpotlight
import Foundation
import SwiftUI

// Synthetic compiler fixture. No application source, accounts or user data.
struct FixtureEntity: AppEntity {
    static let typeDisplayRepresentation: TypeDisplayRepresentation = "Fixture"
    static let defaultQuery = FixtureQuery()

    let id: UUID
    let title: String

    var displayRepresentation: DisplayRepresentation {
        DisplayRepresentation(title: "\(title)")
    }
}

struct FixtureQuery: EntityQuery, EntityStringQuery {
    func entities(for identifiers: [UUID]) async throws -> [FixtureEntity] { [] }
    func entities(matching string: String) async throws -> [FixtureEntity] { [] }
    func suggestedEntities() async throws -> [FixtureEntity] { [] }
}

struct FixtureIntent: AppIntent {
    static let title: LocalizedStringResource = "Fixture Action"

    @Parameter(title: "Item")
    var item: FixtureEntity

    func perform() async throws -> some IntentResult { .result() }
}

#if ORACLE_INDEXED
extension FixtureEntity: IndexedEntity {}
#endif

#if ORACLE_LONG_RUNNING
@available(iOS 27.0, *)
extension FixtureIntent: LongRunningIntent {}
#endif

@main
struct OracleFixture: App {
    var body: some Scene {
        WindowGroup { Text("Synthetic metadata fixture") }
    }
}
