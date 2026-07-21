import AppKit
import Foundation
import SwiftUI
import UserNotifications

struct AgentStatusSnapshot: Decodable {
    let generatedAt: Int
    let runningCount: Int
    let totalRssMb: Int
    let enabledLoopCount: Int
    let runningLoopCount: Int
    let readyLoopCount: Int
    let loops: [LoopStatus]
    let recentResults: [UsefulResult]
    let recentFailures: [UsefulResult]?
    let agents: [AgentStatus]

    enum CodingKeys: String, CodingKey {
        case generatedAt = "generated_at"
        case runningCount = "running_count"
        case totalRssMb = "total_rss_mb"
        case enabledLoopCount = "enabled_loop_count"
        case runningLoopCount = "running_loop_count"
        case readyLoopCount = "ready_loop_count"
        case loops
        case recentResults = "recent_results"
        case recentFailures = "recent_failures"
        case agents
    }
}

struct AgentStatus: Decodable, Identifiable {
    let taskId: String
    let name: String
    let description: String
    let status: String
    let work: String
    let pid: Int
    let elapsed: String
    let rssMb: Int

    var id: String { taskId }

    enum CodingKeys: String, CodingKey {
        case taskId = "task_id"
        case name
        case description
        case status
        case work
        case pid
        case elapsed
        case rssMb = "rss_mb"
    }
}

struct LoopStatus: Decodable, Identifiable {
    let id: String
    let name: String
    let description: String
    let status: String
    let enabled: Bool
    let intervalS: Double
    let threshold: Int
    let work: String
    let lastAge: String
    let backlogCount: Int
    let backlogLabel: String
    let runningAgentCount: Int
    let rssMb: Int

    enum CodingKeys: String, CodingKey {
        case id
        case name
        case description
        case status
        case enabled
        case intervalS = "interval_s"
        case threshold
        case work
        case lastAge = "last_age"
        case backlogCount = "backlog_count"
        case backlogLabel = "backlog_label"
        case runningAgentCount = "running_agent_count"
        case rssMb = "rss_mb"
    }
}

struct UsefulResult: Decodable, Identifiable {
    let id: String
    let taskId: String
    let loopName: String
    let title: String
    let summary: String
    let age: String
    // When false, the item is listed in the menu but no banner is posted — the
    // per-category Notifications toggles gate delivery server-side. Absent in
    // older payloads, where every result was notifiable.
    let notify: Bool?

    var shouldNotify: Bool { notify ?? true }

    enum CodingKeys: String, CodingKey {
        case id
        case taskId = "task_id"
        case loopName = "loop_name"
        case title
        case summary
        case age
        case notify
    }
}

private let panelWidth: CGFloat = 380
private let settingsWindowWidth: CGFloat = 980
private let settingsWindowHeight: CGFloat = 760
private let statusPollInterval: TimeInterval = 120.0
private let disableBackgroundDaemonsKey = "THREADKEEPER_DISABLE_BG_DAEMONS"

private func threadKeeperEnvFileURL() -> URL {
    let env = ProcessInfo.processInfo.environment
    let raw = env["THREADKEEPER_ENV_FILE"]?.trimmingCharacters(in: .whitespacesAndNewlines)
    let path = raw?.isEmpty == false
        ? raw!
        : "~/.threadkeeper/.env"
    return URL(fileURLWithPath: (path as NSString).expandingTildeInPath)
}

enum EnvSettingsSection: String, CaseIterable, Identifiable {
    case cliAgents = "CLI Agents"
    case learningAgents = "Learning Loop Agents"
    case automation = "System Automation"
    case memory = "Memory & Budgets"
    case notifications = "Notifications"
    case advanced = "Advanced .env"

    var id: String { rawValue }

    var icon: String {
        switch self {
        case .cliAgents: return "terminal"
        case .learningAgents: return "brain.head.profile"
        case .automation: return "gearshape.2"
        case .memory: return "memorychip"
        case .notifications: return "bell.badge"
        case .advanced: return "doc.text"
        }
    }
}

enum EnvSettingKind {
    case toggle
    case choice([ChoiceOption])
    case model([ChoiceOption])
}

struct SettingsCatalogSnapshot: Decodable {
    let generatedAt: Int
    let activeCLI: String?
    let clis: [CLICatalogEntry]
    let agentRoles: [LearningAgentDefinition]
    let mechanicalJobs: [MechanicalJobDefinition]
    let runtimeOverrides: [RuntimeSpawnOverride]
    let warnings: [String]

    enum CodingKeys: String, CodingKey {
        case generatedAt = "generated_at"
        case activeCLI = "active_cli"
        case clis
        case agentRoles = "agent_roles"
        case mechanicalJobs = "mechanical_jobs"
        case runtimeOverrides = "runtime_overrides"
        case warnings
    }
}

struct RuntimeSpawnOverride: Decodable, Identifiable {
    let key: String
    let value: String
    let source: String

    var id: String { key }
}

struct CLICatalogEntry: Decodable, Identifiable {
    let id: String
    let name: String
    let installed: Bool
    let executable: String
    let version: String
    let models: [String]
    let modelSource: String
    let sourceUpdatedAt: Int?
    let catalogRefreshedAt: Int
    let catalogAgeS: Int
    let stale: Bool
    let error: String?
    let configuredModel: String
    let configuredModelInCatalog: Bool
    let supportsCustomModel: Bool
    let effortOptions: [String]
    let modelEfforts: [String: [String]]
    let effortMode: String
    let effortNote: String
    let configuredEffort: String
    let latestVersion: String?
    let latestVersionSource: String?
    let releaseURL: String?
    let versionCheckError: String?
    let updateAvailable: Bool?
    let updateSupported: Bool?
    let updateCommandLabel: String?

    enum CodingKeys: String, CodingKey {
        case id, name, installed, executable, version, models, stale, error
        case modelSource = "model_source"
        case sourceUpdatedAt = "source_updated_at"
        case catalogRefreshedAt = "catalog_refreshed_at"
        case catalogAgeS = "catalog_age_s"
        case configuredModel = "configured_model"
        case configuredModelInCatalog = "configured_model_in_catalog"
        case supportsCustomModel = "supports_custom_model"
        case effortOptions = "effort_options"
        case modelEfforts = "model_efforts"
        case effortMode = "effort_mode"
        case effortNote = "effort_note"
        case configuredEffort = "configured_effort"
        case latestVersion = "latest_version"
        case latestVersionSource = "latest_version_source"
        case releaseURL = "release_url"
        case versionCheckError = "version_check_error"
        case updateAvailable = "update_available"
        case updateSupported = "update_supported"
        case updateCommandLabel = "update_command_label"
    }
}

struct CLIUpdateResult: Decodable {
    let cli: String
    let updated: Bool
    let currentVersion: String
    let latestVersion: String
    let command: String
    let message: String
    let output: String

    enum CodingKeys: String, CodingKey {
        case cli, updated, command, message, output
        case currentVersion = "current_version"
        case latestVersion = "latest_version"
    }
}

struct LearningAgentDefinition: Decodable, Identifiable {
    let role: String
    let name: String
    let description: String
    let reads: String
    let writes: String
    let impact: String
    let intervalKey: String
    let cli: String
    let cliSource: String
    let model: String
    let modelSource: String
    let effort: String
    let effortSource: String
    let cliSourceKey: String
    let modelSourceKey: String
    let effortSourceKey: String
    let cliDynamic: Bool
    let cliInherited: Bool
    let modelInherited: Bool
    let effortInherited: Bool

    var id: String { role }

    enum CodingKeys: String, CodingKey {
        case role, name, description, reads, writes, impact, cli, model, effort
        case intervalKey = "interval_key"
        case cliSource = "cli_source"
        case modelSource = "model_source"
        case effortSource = "effort_source"
        case cliSourceKey = "cli_source_key"
        case modelSourceKey = "model_source_key"
        case effortSourceKey = "effort_source_key"
        case cliDynamic = "cli_dynamic"
        case cliInherited = "cli_inherited"
        case modelInherited = "model_inherited"
        case effortInherited = "effort_inherited"
    }
}

struct MechanicalJobDefinition: Decodable, Identifiable {
    let id: String
    let name: String
    let description: String
    let intervalKey: String

    enum CodingKeys: String, CodingKey {
        case id, name, description
        case intervalKey = "interval_key"
    }
}

struct ChoiceOption: Hashable {
    let label: String
    let value: String

    init(_ value: String, label: String? = nil) {
        self.value = value
        self.label = label ?? value
    }
}

struct EnvSettingDefinition: Identifiable {
    let group: String
    let key: String
    let title: String
    let detail: String
    let defaultValue: String
    let kind: EnvSettingKind

    var id: String { key }
}

private func choiceOptions(_ values: [String], preserving preserved: [String] = []) -> [ChoiceOption] {
    var seen: Set<String> = []
    return (values + preserved).compactMap { value in
        let cleaned = value.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !cleaned.isEmpty, seen.insert(cleaned).inserted else { return nil }
        return ChoiceOption(cleaned)
    }
}

private let hourScheduleChoices = [
    ChoiceOption("0", label: "Off"),
    ChoiceOption("30", label: "0.008 h"),
    ChoiceOption("60", label: "0.017 h"),
    ChoiceOption("300", label: "0.083 h"),
    ChoiceOption("900", label: "0.25 h"),
    ChoiceOption("1800", label: "0.5 h"),
    ChoiceOption("3600", label: "1 h"),
    ChoiceOption("10800", label: "3 h"),
    ChoiceOption("21600", label: "6 h"),
    ChoiceOption("43200", label: "12 h"),
    ChoiceOption("86400", label: "24 h · 1 day"),
    ChoiceOption("172800", label: "48 h · 2 days"),
    ChoiceOption("259200", label: "72 h · 3 days"),
    ChoiceOption("604800", label: "168 h · 7 days"),
    ChoiceOption("2592000", label: "720 h · 30 days"),
]

private let notifyPollChoices = [
    ChoiceOption("0", label: "Off"),
    ChoiceOption("30", label: "30 s"),
    ChoiceOption("60", label: "60 s"),
    ChoiceOption("120", label: "2 min"),
    ChoiceOption("300", label: "5 min"),
]

private let notifyChannelChoices = [
    ChoiceOption("macos,log", label: "Banner + log"),
    ChoiceOption("macos", label: "Banner only"),
    ChoiceOption("log", label: "Log only"),
]

private let notifyCooldownChoices = [
    ChoiceOption("0", label: "No cooldown"),
    ChoiceOption("300", label: "5 min"),
    ChoiceOption("900", label: "15 min"),
    ChoiceOption("1800", label: "30 min"),
    ChoiceOption("3600", label: "1 h"),
    ChoiceOption("10800", label: "3 h"),
    ChoiceOption("21600", label: "6 h"),
]

private let scheduleDefaultValues: [String: String] = [
    "THREADKEEPER_INGEST_INTERVAL_S": "3",
    "THREADKEEPER_RETENTION_INTERVAL_S": "0",
    "THREADKEEPER_AUTO_UPDATE_INTERVAL_S": "86400",
    "THREADKEEPER_SKILL_UPDATE_INTERVAL_S": "302400",
    "THREADKEEPER_SHADOW_REVIEW_INTERVAL_S": "0",
    "THREADKEEPER_CURATOR_INTERVAL_S": "259200",
    "THREADKEEPER_EXTRACT_INTERVAL_S": "0",
    "THREADKEEPER_CANDIDATE_REVIEW_INTERVAL_S": "0",
    "THREADKEEPER_PROBE_INTERVAL_S": "0",
    "THREADKEEPER_EVOLVE_REVIEW_INTERVAL_S": "0",
    "THREADKEEPER_EVOLVE_APPLY_INTERVAL_S": "0",
    "THREADKEEPER_THREAD_JANITOR_INTERVAL_S": "0",
    "THREADKEEPER_DIALECTIC_MINE_INTERVAL_S": "0",
    "THREADKEEPER_DIALECTIC_VALIDATE_INTERVAL_S": "0",
]

private let memoryLimitChoices = choiceOptions([
    "0", "256", "512", "768", "1024", "1536", "2048", "3072",
    "4096", "6144", "8192", "12288",
])

private let eventCountChoices = choiceOptions([
    "1", "2", "5", "10", "20", "50", "100",
])

private let serverCountChoices = choiceOptions([
    "1", "2", "3", "4", "6", "8",
])

private let baseEnvSettingDefinitions: [EnvSettingDefinition] = [
    EnvSettingDefinition(
        group: "Core",
        key: disableBackgroundDaemonsKey,
        title: "Disable background daemons",
        detail: "Stops autonomous loops for one-shot or debug sessions.",
        defaultValue: "false",
        kind: .toggle
    ),
    EnvSettingDefinition(
        group: "Core",
        key: "THREADKEEPER_NO_EMBEDDINGS",
        title: "Disable embeddings",
        detail: "Uses FTS-only search when memory is tight.",
        defaultValue: "false",
        kind: .toggle
    ),
    EnvSettingDefinition(
        group: "Core",
        key: "THREADKEEPER_MENUBAR_AUTO_LAUNCH",
        title: "Menu-bar auto launch",
        detail: "Installs and opens this helper when the MCP server starts.",
        defaultValue: "true",
        kind: .toggle
    ),
    EnvSettingDefinition(
        group: "Core",
        key: "THREADKEEPER_MENUBAR_RESTART_RSS_MB",
        title: "Menu-bar restart RSS",
        detail: "Restarts this helper at the selected MB threshold; 0 disables.",
        defaultValue: "1024",
        kind: .choice(memoryLimitChoices)
    ),
    EnvSettingDefinition(
        group: "Core",
        key: "THREADKEEPER_AUTO_UPDATE_INTERVAL_S",
        title: "Auto-update interval (hours)",
        detail: "Choose the cadence in hours; Off disables package checks.",
        defaultValue: "86400",
        kind: .choice(hourScheduleChoices)
    ),
    EnvSettingDefinition(
        group: "Core",
        key: "THREADKEEPER_AUTO_UPDATE_RESTART",
        title: "Restart after update",
        detail: "Exits the MCP server after an update so the host reloads it.",
        defaultValue: "true",
        kind: .toggle
    ),
    EnvSettingDefinition(
        group: "Core",
        key: "THREADKEEPER_AUTO_UPDATE_VERIFY_PROVENANCE",
        title: "Verify PyPI provenance",
        detail: "Requires trusted PyPI attestations before packaged self-updates.",
        defaultValue: "true",
        kind: .toggle
    ),

    EnvSettingDefinition(
        group: "Learning Loops",
        key: "THREADKEEPER_SHADOW_REVIEW_INTERVAL_S",
        title: "Shadow review interval",
        detail: "Reviews closed work for memory or skill candidates.",
        defaultValue: "0",
        kind: .choice(hourScheduleChoices)
    ),
    EnvSettingDefinition(
        group: "Learning Loops",
        key: "THREADKEEPER_CURATOR_INTERVAL_S",
        title: "Curator interval",
        detail: "Audits and consolidates materialized lessons.",
        defaultValue: "259200",
        kind: .choice(hourScheduleChoices)
    ),
    EnvSettingDefinition(
        group: "Learning Loops",
        key: "THREADKEEPER_EXTRACT_INTERVAL_S",
        title: "Extract interval",
        detail: "Mines recent dialog for structured memory candidates.",
        defaultValue: "0",
        kind: .choice(hourScheduleChoices)
    ),
    EnvSettingDefinition(
        group: "Learning Loops",
        key: "THREADKEEPER_CANDIDATE_REVIEW_INTERVAL_S",
        title: "Candidate review interval",
        detail: "Promotes or rejects extracted candidates.",
        defaultValue: "0",
        kind: .choice(hourScheduleChoices)
    ),
    EnvSettingDefinition(
        group: "Learning Loops",
        key: "THREADKEEPER_EVOLVE_REVIEW_INTERVAL_S",
        title: "Evolve review interval",
        detail: "Reviews proposed code or docs evolution work.",
        defaultValue: "0",
        kind: .choice(hourScheduleChoices)
    ),
    EnvSettingDefinition(
        group: "Learning Loops",
        key: "THREADKEEPER_EVOLVE_APPLY_INTERVAL_S",
        title: "Evolve apply interval",
        detail: "Applies promoted evolve work; keep deliberate.",
        defaultValue: "0",
        kind: .choice(hourScheduleChoices)
    ),
    EnvSettingDefinition(
        group: "Learning Loops",
        key: "THREADKEEPER_THREAD_JANITOR_INTERVAL_S",
        title: "Thread janitor interval",
        detail: "Closes idle working-memory threads.",
        defaultValue: "0",
        kind: .choice(hourScheduleChoices)
    ),
    EnvSettingDefinition(
        group: "Learning Loops",
        key: "THREADKEEPER_PROBE_INTERVAL_S",
        title: "Probe interval",
        detail: "Runs reliability probes at the selected hourly cadence.",
        defaultValue: "0",
        kind: .choice(hourScheduleChoices)
    ),
    EnvSettingDefinition(
        group: "Learning Loops",
        key: "THREADKEEPER_DIALECTIC_MINE_INTERVAL_S",
        title: "Dialectic mine interval",
        detail: "Finds candidate claims from dialog.",
        defaultValue: "0",
        kind: .choice(hourScheduleChoices)
    ),
    EnvSettingDefinition(
        group: "Learning Loops",
        key: "THREADKEEPER_DIALECTIC_VALIDATE_INTERVAL_S",
        title: "Dialectic validate interval",
        detail: "Validates queued dialectic claims.",
        defaultValue: "0",
        kind: .choice(hourScheduleChoices)
    ),

    EnvSettingDefinition(
        group: "Memory And Budgets",
        key: "THREADKEEPER_MEMORY_NUDGE_INTERVAL",
        title: "Memory nudge interval",
        detail: "Number of events before the memory-save nudge appears.",
        defaultValue: "10",
        kind: .choice(eventCountChoices)
    ),
    EnvSettingDefinition(
        group: "Memory And Budgets",
        key: "THREADKEEPER_SKILL_NUDGE_INTERVAL",
        title: "Skill nudge interval",
        detail: "Number of events before skill-materialization checks.",
        defaultValue: "10",
        kind: .choice(eventCountChoices)
    ),
    EnvSettingDefinition(
        group: "Memory And Budgets",
        key: "THREADKEEPER_SPAWN_BUDGET_MB",
        title: "Spawn budget",
        detail: "Aggregate child-agent RSS budget in MB; 0 disables.",
        defaultValue: "3072",
        kind: .choice(memoryLimitChoices)
    ),
    EnvSettingDefinition(
        group: "Memory And Budgets",
        key: "THREADKEEPER_MEMORY_GUARD_WARN_MB",
        title: "Memory guard warn",
        detail: "Warn threshold per server process, in MB.",
        defaultValue: "1536",
        kind: .choice(memoryLimitChoices)
    ),
    EnvSettingDefinition(
        group: "Memory And Budgets",
        key: "THREADKEEPER_MEMORY_GUARD_KILL_MB",
        title: "Memory guard kill",
        detail: "Kill threshold per server process, in MB.",
        defaultValue: "3072",
        kind: .choice(memoryLimitChoices)
    ),
    EnvSettingDefinition(
        group: "Memory And Budgets",
        key: "THREADKEEPER_MEMORY_GUARD_TARGET_SERVERS",
        title: "Target server count",
        detail: "Number of MCP server processes to keep after reclaim.",
        defaultValue: "1",
        kind: .choice(serverCountChoices)
    ),
    EnvSettingDefinition(
        group: "Notifications",
        key: "THREADKEEPER_NOTIFY_POLL_S",
        title: "Notifications",
        detail: "Master switch. How often to check loop health; Off disables all notifications.",
        defaultValue: "0",
        kind: .choice(notifyPollChoices)
    ),
    EnvSettingDefinition(
        group: "Notifications",
        key: "THREADKEEPER_NOTIFY_CHANNEL",
        title: "Delivery channel",
        detail: "Native macOS banner, a log line, or both.",
        defaultValue: "macos,log",
        kind: .choice(notifyChannelChoices)
    ),
    EnvSettingDefinition(
        group: "Notifications",
        key: "THREADKEEPER_NOTIFY_FAILURE_COOLDOWN_S",
        title: "Failure cooldown",
        detail: "Minimum gap between repeats of one loop's failure, so a lapsed subscription is one alert, not a storm.",
        defaultValue: "3600",
        kind: .choice(notifyCooldownChoices)
    ),
    EnvSettingDefinition(
        group: "Notifications",
        key: "THREADKEEPER_NOTIFY_LOOP_FAILURE",
        title: "Loop / spawn failure",
        detail: "Alert when a background loop can't bring up its model/CLI (credits, auth, missing binary) or a child dies mid-run.",
        defaultValue: "true",
        kind: .toggle
    ),
    EnvSettingDefinition(
        group: "Notifications",
        key: "THREADKEEPER_NOTIFY_SKILL_MATERIALIZED",
        title: "Skill materialized",
        detail: "Alert when a skill is captured.",
        defaultValue: "false",
        kind: .toggle
    ),
    EnvSettingDefinition(
        group: "Notifications",
        key: "THREADKEEPER_NOTIFY_LESSON",
        title: "Lesson added",
        detail: "Alert when a lesson is appended.",
        defaultValue: "false",
        kind: .toggle
    ),
]
struct EnvPresetSlot: Codable, Identifiable {
    let slot: Int
    var name: String
    var values: [String: String]
    var rawEnvText: String

    var id: Int { slot }

    static func defaults() -> [EnvPresetSlot] {
        (1...3).map {
            EnvPresetSlot(slot: $0, name: "Preset \($0)", values: [:], rawEnvText: "")
        }
    }
}

@MainActor
final class EnvSettingsStore: ObservableObject {
    @Published var values: [String: String] = [:]
    @Published var rawEnvText = ""
    @Published var presetSlots: [EnvPresetSlot] = EnvPresetSlot.defaults()
    @Published var validationMessages: [String] = []
    @Published var statusMessage = ""
    @Published var isSaving = false
    @Published var isLoaded = false
    @Published var catalog: SettingsCatalogSnapshot?
    @Published var isCatalogLoading = false
    @Published var catalogError = ""
    @Published var configurationWarnings: [String] = []
    @Published var isDraftDirty = false
    @Published var updatingCLIID = ""
    @Published var cliUpdateMessage = ""

    private weak var agentStore: AgentStatusStore?
    private let presetDefaultsKey = "threadkeeperEnvPresetSlotsV1"
    let envFileURL: URL

    var canSave: Bool {
        validationMessages.isEmpty && !isSaving
    }

    var envPath: String {
        envFileURL.path
    }

    var isThreadKeeperDisabled: Bool {
        Self.boolValue(values[disableBackgroundDaemonsKey] ?? "") ?? false
    }

    var cliChoices: [ChoiceOption] {
        (catalog?.clis ?? []).map { ChoiceOption($0.id, label: $0.name) }
    }

    var allDefinitions: [EnvSettingDefinition] {
        var definitions = baseEnvSettingDefinitions
        let clis = catalog?.clis ?? []
        let choices = cliChoices
        let activeCLI = catalog?.activeCLI
            ?? clis.first(where: \.installed)?.id
            ?? "claude"
        if !choices.isEmpty {
            definitions.append(EnvSettingDefinition(
                group: "CLI Agents",
                key: "THREADKEEPER_SPAWN__DEFAULT",
                title: "Default CLI",
                detail: "Inherited by every Learning Loop agent without an override.",
                defaultValue: activeCLI,
                kind: .choice(choices)
            ))
        }
        for cli in clis {
            let token = cli.id.uppercased()
            let modelKey = "THREADKEEPER_SPAWN__MODEL__\(token)"
            let effortKey = "THREADKEEPER_SPAWN__EFFORT__\(token)"
            definitions.append(EnvSettingDefinition(
                group: "CLI Agents",
                key: modelKey,
                title: "\(cli.name) default model",
                detail: "CLI default when an agent has no model override.",
                defaultValue: "CLI managed",
                kind: .model(choiceOptions(
                    cli.models,
                    preserving: [values[modelKey] ?? ""]
                ))
            ))
            if cli.effortMode == "independent" {
                let efforts = effortOptions(
                    for: cli,
                    model: values[modelKey] ?? ""
                )
                definitions.append(EnvSettingDefinition(
                    group: "CLI Agents",
                    key: effortKey,
                    title: "\(cli.name) default effort",
                    detail: "CLI effort inherited by agents without an override.",
                    defaultValue: "CLI managed",
                    kind: .choice(choiceOptions(
                        efforts,
                        preserving: [values[effortKey] ?? ""]
                    ))
                ))
            }
        }
        for role in catalog?.agentRoles ?? [] {
            let token = role.role.uppercased()
            definitions.append(EnvSettingDefinition(
                group: "Learning Loop Agents",
                key: "THREADKEEPER_SPAWN__LOOP__\(token)",
                title: "\(role.name) CLI",
                detail: "Default inherits the global CLI.",
                defaultValue: role.cli.isEmpty ? activeCLI : role.cli,
                kind: .choice(choices)
            ))
            let roleCLI = draftCLI(for: role)
            let models = catalog?.clis.first(where: { $0.id == roleCLI })?.models ?? []
            let roleModelKey = "THREADKEEPER_SPAWN__MODEL__\(token)"
            definitions.append(EnvSettingDefinition(
                group: "Learning Loop Agents",
                key: roleModelKey,
                title: "\(role.name) model",
                detail: "Default inherits the selected CLI model.",
                defaultValue: role.model.isEmpty ? "CLI managed" : role.model,
                kind: .model(choiceOptions(
                    models,
                    preserving: [values[roleModelKey] ?? ""]
                ))
            ))
            // Keep every role effort key in the canonical definition set even
            // before DEFAULT / role CLI values have been hydrated from raw
            // .env. Rendering remains conditional in LearningAgentCard, but
            // cold-load extraction must never miss and later erase an effort
            // override merely because its CLI was not known on the first pass.
            let selectedCLI = catalog?.clis.first(where: { $0.id == roleCLI })
            let roleModel = draftModel(for: role)
            let efforts = selectedCLI.map {
                effortOptions(for: $0, model: roleModel)
            } ?? []
            let effortKey = "THREADKEEPER_SPAWN__EFFORT__\(token)"
            let effortKind: EnvSettingKind
            if selectedCLI?.effortMode == "independent" {
                effortKind = .choice(choiceOptions(
                    efforts,
                    preserving: [values[effortKey] ?? ""]
                ))
            } else {
                // Antigravity encodes effort in its model label. The raw key
                // stays known/preserved while its independent picker is hidden.
                effortKind = .choice([])
            }
            definitions.append(EnvSettingDefinition(
                group: "Learning Loop Agents",
                key: effortKey,
                title: "\(role.name) effort",
                detail: "Default inherits the selected CLI effort.",
                defaultValue: role.effort.isEmpty ? "CLI managed" : role.effort,
                kind: effortKind
            ))
            if !role.intervalKey.isEmpty {
                definitions.append(EnvSettingDefinition(
                    group: "Learning Loop Agents",
                    key: role.intervalKey,
                    title: "\(role.name) schedule (hours)",
                    detail: "Choose the cadence in hours; Off disables this schedule.",
                    defaultValue: scheduleDefaultValues[role.intervalKey] ?? "0",
                    kind: .choice(hourScheduleChoices)
                ))
            }
        }
        for job in catalog?.mechanicalJobs ?? [] {
            definitions.append(EnvSettingDefinition(
                group: "System Automation",
                key: job.intervalKey,
                title: "\(job.name) schedule (hours)",
                detail: "Choose the cadence in hours; Off disables this job.",
                defaultValue: scheduleDefaultValues[job.intervalKey] ?? "0",
                kind: .choice(hourScheduleChoices)
            ))
        }
        var seen: Set<String> = []
        return definitions.filter { seen.insert($0.key).inserted }
    }

    init(agentStore: AgentStatusStore?) {
        self.agentStore = agentStore
        self.envFileURL = Self.resolveEnvFileURL()
        loadPresetSlots()
        statusMessage = "Ready."
    }

    func definition(for key: String) -> EnvSettingDefinition? {
        allDefinitions.first { $0.key == key }
    }

    func cli(for id: String) -> CLICatalogEntry? {
        catalog?.clis.first { $0.id == id }
    }

    func hasRuntimeOverride(for role: LearningAgentDefinition) -> Bool {
        let token = role.role.uppercased()
        let roleKeys: Set<String> = [
            "THREADKEEPER_SPAWN__LOOP__\(token)",
            "THREADKEEPER_SPAWN__MODEL__\(token)",
            "THREADKEEPER_SPAWN__EFFORT__\(token)",
        ]
        return (catalog?.runtimeOverrides ?? []).contains {
            roleKeys.contains($0.key.uppercased())
        }
    }

    func effortOptions(for cli: CLICatalogEntry, model: String) -> [String] {
        let selected = model.trimmingCharacters(in: .whitespacesAndNewlines)
        if !selected.isEmpty, let specific = cli.modelEfforts[selected], !specific.isEmpty {
            return specific
        }
        return cli.effortOptions
    }

    func draftCLI(for role: LearningAgentDefinition) -> String {
        let roleKey = "THREADKEEPER_SPAWN__LOOP__\(role.role.uppercased())"
        if let explicit = values[roleKey], !explicit.isEmpty { return explicit }
        if let fallback = values["THREADKEEPER_SPAWN__DEFAULT"], !fallback.isEmpty {
            return fallback
        }
        return ""
    }

    func draftModel(for role: LearningAgentDefinition) -> String {
        let roleKey = "THREADKEEPER_SPAWN__MODEL__\(role.role.uppercased())"
        if let explicit = values[roleKey], !explicit.isEmpty { return explicit }
        let cli = draftCLI(for: role)
        let cliKey = "THREADKEEPER_SPAWN__MODEL__\(cli.uppercased())"
        if let inherited = values[cliKey], !inherited.isEmpty { return inherited }
        return "CLI default"
    }

    func draftEffort(for role: LearningAgentDefinition) -> String {
        let roleKey = "THREADKEEPER_SPAWN__EFFORT__\(role.role.uppercased())"
        if let explicit = values[roleKey], !explicit.isEmpty { return explicit }
        let cli = draftCLI(for: role)
        let cliKey = "THREADKEEPER_SPAWN__EFFORT__\(cli.uppercased())"
        if let inherited = values[cliKey], !inherited.isEmpty { return inherited }
        return "CLI default"
    }

    func binding(for key: String) -> Binding<String> {
        Binding(
            get: { self.values[key] ?? "" },
            set: { self.setValue($0, for: key) }
        )
    }

    func rawDraftBinding() -> Binding<String> {
        Binding(
            get: { self.rawEnvText },
            set: { newValue in
                self.rawEnvText = newValue
                self.isDraftDirty = true
                self.values = Self.extractKnownValues(
                    from: newValue,
                    definitions: self.allDefinitions
                )
                self.validate()
            }
        )
    }

    func setValue(_ value: String, for key: String) {
        let cleaned = value.replacingOccurrences(of: "\n", with: " ")
        if cleaned.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            values.removeValue(forKey: key)
        } else {
            values[key] = cleaned.trimmingCharacters(in: .whitespacesAndNewlines)
        }
        isDraftDirty = true
        if isLoaded {
            rawEnvText = Self.mergeEnvText(
                raw: rawEnvText,
                values: values,
                definitions: allDefinitions
            )
        }
        validate()
    }

    func loadEnv() {
        do {
            if FileManager.default.fileExists(atPath: envFileURL.path) {
                rawEnvText = try String(contentsOf: envFileURL, encoding: .utf8)
                values = Self.extractKnownValues(from: rawEnvText, definitions: allDefinitions)
                statusMessage = "Loaded \(envFileURL.path)"
            } else {
                rawEnvText = ""
                values = [:]
                statusMessage = "A new .env will be created at \(envFileURL.path)"
            }
        } catch {
            statusMessage = "Could not read .env: \(error.localizedDescription)"
        }
        isLoaded = true
        isDraftDirty = false
        validate()
        refreshCatalog()
    }

    func reloadEnvWithConfirmation() {
        guard confirmDiscardDraft(
            title: "Reload .env?",
            message: "Reloading replaces unsaved Guided and Advanced edits with the file on disk."
        ) else { return }
        loadEnv()
    }

    func reloadOnWindowPresentation() {
        if isDraftDirty {
            statusMessage = "Reopened with unsaved settings draft preserved."
            return
        }
        loadEnv()
    }

    func refreshCatalog(force: Bool = false) {
        guard !isCatalogLoading, let agentStore else { return }
        isCatalogLoading = true
        catalogError = ""
        let arguments = force
            ? ["--settings-catalog", "--refresh-catalog"]
            : ["--settings-catalog"]
        Task { [weak self] in
            let result = await Task.detached { [agentStore, arguments] in
                do {
                    return (try agentStore.runStatusCommand(arguments: arguments), "")
                } catch {
                    return (Data(), error.localizedDescription)
                }
            }.value
            guard let self else { return }
            if !result.1.isEmpty {
                self.isCatalogLoading = false
                self.catalogError = result.1
                self.statusMessage = "CLI capability refresh failed."
                return
            }
            do {
                let decoded = try JSONDecoder().decode(
                    SettingsCatalogSnapshot.self, from: result.0
                )
                self.catalog = decoded
                self.isCatalogLoading = false
                let catalogValues = Self.extractKnownValues(
                    from: self.rawEnvText,
                    definitions: self.allDefinitions
                )
                // Catalog refresh changes available choices only. Preserve
                // every unsaved Guided/custom selection and fill only keys
                // that became known after the initial catalog arrived.
                for (key, value) in catalogValues where self.values[key] == nil {
                    self.values[key] = value
                }
                self.validate()
                self.statusMessage = "Loaded current CLI capabilities."
            } catch {
                self.isCatalogLoading = false
                self.catalogError = error.localizedDescription
                self.statusMessage = "CLI capability refresh failed."
            }
        }
    }

    func updateCLI(_ cli: CLICatalogEntry) {
        guard cli.updateAvailable == true, updatingCLIID.isEmpty else { return }
        guard cli.updateSupported == true else {
            cliUpdateMessage = "No supported updater was detected for \(cli.name)."
            statusMessage = cliUpdateMessage
            return
        }
        guard confirmCLIUpdate(cli), let agentStore else { return }
        updatingCLIID = cli.id
        cliUpdateMessage = "Updating \(cli.name)…"
        statusMessage = cliUpdateMessage
        Task { [weak self] in
            let result = await Task.detached { [agentStore] in
                do {
                    let data = try agentStore.runStatusCommand(
                        arguments: ["--update-cli", cli.id]
                    )
                    let decoded = try JSONDecoder().decode(CLIUpdateResult.self, from: data)
                    return (decoded.message, "")
                } catch {
                    return ("", error.localizedDescription)
                }
            }.value
            guard let self else { return }
            self.updatingCLIID = ""
            if result.1.isEmpty {
                self.cliUpdateMessage = result.0
                self.statusMessage = result.0
                self.refreshCatalog(force: true)
            } else {
                self.cliUpdateMessage = "\(cli.name) update failed: \(result.1)"
                self.statusMessage = self.cliUpdateMessage
            }
        }
    }

    private func confirmCLIUpdate(_ cli: CLICatalogEntry) -> Bool {
        let current = cli.version.isEmpty ? "unknown" : cli.version
        let latest = cli.latestVersion?.isEmpty == false ? cli.latestVersion! : "latest"
        let command = cli.updateCommandLabel?.isEmpty == false
            ? "\n\nUpdater: \(cli.updateCommandLabel!)"
            : ""
        let alert = NSAlert()
        alert.alertStyle = .informational
        alert.messageText = "Update \(cli.name)?"
        alert.informativeText = "Current: \(current)\nLatest: \(latest)\(command)"
        alert.addButton(withTitle: "Update")
        alert.addButton(withTitle: "Cancel")
        return alert.runModal() == .alertFirstButtonReturn
    }

    func importRawIntoForm() {
        values = Self.extractKnownValues(from: rawEnvText, definitions: allDefinitions)
        validate()
        statusMessage = "Imported raw .env values into the form."
    }

    func syncFormToRaw() {
        rawEnvText = Self.mergeEnvText(raw: rawEnvText, values: values, definitions: allDefinitions)
        validate()
        statusMessage = "Updated the raw .env preview from the form."
    }

    func reconcileDraft(leavingAdvanced: Bool) {
        if leavingAdvanced {
            values = Self.extractKnownValues(from: rawEnvText, definitions: allDefinitions)
        } else {
            rawEnvText = Self.mergeEnvText(
                raw: rawEnvText,
                values: values,
                definitions: allDefinitions
            )
        }
        validate()
    }

    @discardableResult
    func saveDraft(editingAdvanced: Bool, restart: Bool) -> Bool {
        reconcileDraft(leavingAdvanced: editingAdvanced)
        // Always persist the reconciled canonical draft. This prevents either
        // sidebar from saving stale state owned by the other representation.
        return save(restart: restart)
    }

    @discardableResult
    func save(restart: Bool) -> Bool {
        validate()
        guard canSave else {
            statusMessage = "Fix the highlighted settings before saving."
            return false
        }

        isSaving = true
        defer {
            isSaving = false
        }

        do {
            let merged = Self.mergeEnvText(raw: rawEnvText, values: values, definitions: allDefinitions)
            try FileManager.default.createDirectory(
                at: envFileURL.deletingLastPathComponent(),
                withIntermediateDirectories: true
            )
            try merged.write(to: envFileURL, atomically: true, encoding: .utf8)
            rawEnvText = merged
            isDraftDirty = false
            statusMessage = "Saved \(envFileURL.path)"

            if restart {
                try requestThreadKeeperRestart()
                statusMessage = "Saved and requested ThreadKeeper restart."
            }
            agentStore?.refreshThreadKeeperToggleState()
            agentStore?.refresh()
            return true
        } catch {
            statusMessage = "Save failed: \(error.localizedDescription)"
            return false
        }
    }

    @discardableResult
    func saveRaw(restart: Bool) -> Bool {
        saveDraft(editingAdvanced: true, restart: restart)
    }

    @discardableResult
    func setThreadKeeperDisabled(_ disabled: Bool, restart: Bool) -> Bool {
        if !isLoaded {
            loadEnv()
        }
        setValue(disabled ? "true" : "false", for: disableBackgroundDaemonsKey)
        return save(restart: restart)
    }

    func renamePreset(slot: Int, name: String) {
        guard let index = presetSlots.firstIndex(where: { $0.slot == slot }) else {
            return
        }
        presetSlots[index].name = name
        savePresetSlots()
    }

    func savePreset(slot: Int) {
        guard let index = presetSlots.firstIndex(where: { $0.slot == slot }) else {
            return
        }
        let merged = Self.mergeEnvText(raw: rawEnvText, values: values, definitions: allDefinitions)
        presetSlots[index].values = values
        presetSlots[index].rawEnvText = merged
        if presetSlots[index].name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            presetSlots[index].name = "Preset \(slot)"
        }
        savePresetSlots()
        statusMessage = "Saved \(presetSlots[index].name)."
    }

    func loadPreset(slot: Int) {
        guard let preset = presetSlots.first(where: { $0.slot == slot }) else {
            return
        }
        rawEnvText = preset.rawEnvText
        values = preset.values.isEmpty
            ? Self.extractKnownValues(from: preset.rawEnvText, definitions: allDefinitions)
            : preset.values
        isDraftDirty = true
        validate()
        statusMessage = "Loaded \(preset.name)."
    }

    func loadPresetWithConfirmation(slot: Int) {
        guard confirmDiscardDraft(
            title: "Load preset?",
            message: "Loading a preset replaces unsaved Guided and Advanced edits."
        ) else { return }
        loadPreset(slot: slot)
    }

    func clearPreset(slot: Int) {
        guard let index = presetSlots.firstIndex(where: { $0.slot == slot }) else {
            return
        }
        presetSlots[index].values = [:]
        presetSlots[index].rawEnvText = ""
        savePresetSlots()
        statusMessage = "Cleared \(presetSlots[index].name)."
    }

    func validate() {
        var messages: [String] = []
        var warnings = catalog?.warnings ?? []
        for definition in allDefinitions {
            guard let rawValue = values[definition.key],
                  !rawValue.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
                continue
            }
            let value = rawValue.trimmingCharacters(in: .whitespacesAndNewlines)
            if value.contains("\n") {
                messages.append("\(definition.title) cannot contain line breaks.")
            }

            switch definition.kind {
            case .toggle:
                if !Self.isBoolValue(value) {
                    messages.append("\(definition.title) must be true or false.")
                }
            case .choice(let choices):
                if !choices.contains(where: { $0.value == value }) {
                    warnings.append(
                        "\(definition.title) keeps custom .env value \"\(value)\"; "
                        + "choose a dropdown option to replace it."
                    )
                }
            case .model:
                break
            }
        }

        for line in rawEnvText.components(separatedBy: .newlines) {
            guard let parsed = Self.parseEnvLine(line), !parsed.isCommented else { continue }
            if parsed.key.hasPrefix("THREADKEEPER_SPAWN__"),
               parsed.key.hasSuffix("__GEMINI") {
                warnings.append(
                    "\(parsed.key) is preserved but unsupported: Gemini Legacy was removed."
                )
            }
        }

        appendThresholdWarning(
            to: &messages,
            warnKey: "THREADKEEPER_MEMORY_GUARD_WARN_MB",
            killKey: "THREADKEEPER_MEMORY_GUARD_KILL_MB",
            label: "Memory guard kill"
        )
        validationMessages = messages
        configurationWarnings = Array(Set(warnings)).sorted()
    }

    private func appendThresholdWarning(
        to messages: inout [String],
        warnKey: String,
        killKey: String,
        label: String
    ) {
        guard let warnRaw = values[warnKey],
              let killRaw = values[killKey],
              let warn = Double(warnRaw),
              let kill = Double(killRaw),
              kill < warn else {
            return
        }
        messages.append("\(label) should be greater than or equal to the warn threshold.")
    }

    private func requestThreadKeeperRestart() throws {
        _ = try? agentStore?.runStatusCommand(arguments: ["--cleanup-memory"])

        let process = Process()
        let errPipe = Pipe()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/pkill")
        process.arguments = ["-TERM", "-f", "threadkeeper.server"]
        process.standardInput = FileHandle.nullDevice
        process.standardOutput = FileHandle.nullDevice
        process.standardError = errPipe
        defer {
            errPipe.fileHandleForReading.closeFile()
        }

        try process.run()
        process.waitUntilExit()
        if process.terminationStatus == 0 || process.terminationStatus == 1 {
            return
        }

        let err = String(
            data: errPipe.fileHandleForReading.readDataToEndOfFile(),
            encoding: .utf8
        ) ?? "pkill failed"
        throw NSError(
            domain: "ThreadKeeperEnvSettings",
            code: Int(process.terminationStatus),
            userInfo: [NSLocalizedDescriptionKey: err.trimmingCharacters(in: .whitespacesAndNewlines)]
        )
    }

    private func loadPresetSlots() {
        guard let data = UserDefaults.standard.data(forKey: presetDefaultsKey),
              let decoded = try? JSONDecoder().decode([EnvPresetSlot].self, from: data),
              decoded.count == 3 else {
            presetSlots = EnvPresetSlot.defaults()
            return
        }
        presetSlots = decoded.sorted { $0.slot < $1.slot }
    }

    private func savePresetSlots() {
        if let encoded = try? JSONEncoder().encode(presetSlots) {
            UserDefaults.standard.set(encoded, forKey: presetDefaultsKey)
        }
    }

    private func confirmDiscardDraft(title: String, message: String) -> Bool {
        guard isDraftDirty else { return true }
        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = title
        alert.informativeText = message
        alert.addButton(withTitle: "Discard Unsaved Changes")
        alert.addButton(withTitle: "Cancel")
        return alert.runModal() == .alertFirstButtonReturn
    }

    private static func resolveEnvFileURL() -> URL {
        threadKeeperEnvFileURL()
    }

    private static func extractKnownValues(
        from text: String,
        definitions: [EnvSettingDefinition]
    ) -> [String: String] {
        let definitionsByKey = Dictionary(
            uniqueKeysWithValues: definitions.map { ($0.key, $0) }
        )
        var parsedValues: [String: String] = [:]
        for line in text.components(separatedBy: .newlines) {
            guard let parsed = parseEnvLine(line),
                  !parsed.isCommented else {
                continue
            }
            let canonicalKey = canonicalSettingKey(parsed.key)
            guard let definition = definitionsByKey[canonicalKey] else { continue }
            // A canonical assignment always wins over the old AGY alias,
            // regardless of their relative order in the file.
            if canonicalKey != parsed.key, parsedValues[canonicalKey] != nil {
                continue
            }
            parsedValues[canonicalKey] = normalizedValue(parsed.value, for: definition)
        }
        return parsedValues
    }

    private static func canonicalSettingKey(_ key: String) -> String {
        switch key {
        case "THREADKEEPER_SPAWN__MODEL__AGY":
            return "THREADKEEPER_SPAWN__MODEL__ANTIGRAVITY"
        case "THREADKEEPER_SPAWN__EFFORT__AGY":
            return "THREADKEEPER_SPAWN__EFFORT__ANTIGRAVITY"
        default:
            return key
        }
    }

    private static func mergeEnvText(
        raw: String,
        values: [String: String],
        definitions: [EnvSettingDefinition]
    ) -> String {
        let knownKeys = Set(definitions.map(\.key))
        let rawLines = raw.components(separatedBy: .newlines)
        let activeCanonicalKeys = Set(rawLines.compactMap { line -> String? in
            guard let parsed = parseEnvLine(line), !parsed.isCommented,
                  knownKeys.contains(parsed.key) else { return nil }
            return parsed.key
        })
        var seen: Set<String> = []
        var output: [String] = []

        for line in rawLines {
            guard let parsed = parseEnvLine(line) else {
                output.append(line)
                continue
            }

            let canonicalKey = canonicalSettingKey(parsed.key)
            guard knownKeys.contains(canonicalKey) else {
                output.append(line)
                continue
            }

            if parsed.isCommented {
                output.append(line)
                continue
            }
            if canonicalKey != parsed.key,
               activeCanonicalKeys.contains(canonicalKey) || seen.contains(canonicalKey) {
                output.append("# Migrated legacy alias: \(line.trimmingCharacters(in: .whitespaces))")
                continue
            }
            seen.insert(canonicalKey)

            let value = values[canonicalKey]?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
            let comment = inlineComment(in: line)
            let commentSuffix = comment.isEmpty ? "" : " \(comment)"
            if value.isEmpty {
                output.append("# \(canonicalKey)=\(commentSuffix)")
            } else {
                output.append("\(canonicalKey)=\(formatEnvValue(value))\(commentSuffix)")
            }
        }

        let missing = definitions.filter {
            !seen.contains($0.key)
                && !(values[$0.key]?.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ?? true)
        }
        if !missing.isEmpty {
            if !output.isEmpty && output.last != "" {
                output.append("")
            }
            output.append("# Updated by ThreadKeeper settings")
            for definition in missing {
                if let value = values[definition.key]?.trimmingCharacters(in: .whitespacesAndNewlines),
                   !value.isEmpty {
                    output.append("\(definition.key)=\(formatEnvValue(value))")
                }
            }
        }

        while output.last == "" {
            output.removeLast()
        }
        return output.isEmpty ? "" : output.joined(separator: "\n") + "\n"
    }

    private static func parseEnvLine(_ line: String) -> (key: String, value: String, isCommented: Bool)? {
        var trimmed = line.trimmingCharacters(in: .whitespaces)
        var isCommented = false
        if trimmed.hasPrefix("#") {
            isCommented = true
            trimmed = String(trimmed.dropFirst()).trimmingCharacters(in: .whitespaces)
        }
        guard let equalsIndex = trimmed.firstIndex(of: "=") else {
            return nil
        }

        let key = String(trimmed[..<equalsIndex]).trimmingCharacters(in: .whitespaces)
        guard isEnvKey(key) else {
            return nil
        }

        let valueStart = trimmed.index(after: equalsIndex)
        let rawValue = String(trimmed[valueStart...]).trimmingCharacters(in: .whitespaces)
        return (key, unquote(stripInlineComment(rawValue)), isCommented)
    }

    private static func isEnvKey(_ key: String) -> Bool {
        guard !key.isEmpty else {
            return false
        }
        let allowed = CharacterSet(charactersIn: "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_")
        return key.unicodeScalars.allSatisfy { allowed.contains($0) }
    }

    private static func stripInlineComment(_ value: String) -> String {
        var inSingle = false
        var inDouble = false
        var escaped = false

        for index in value.indices {
            let char = value[index]
            if escaped {
                escaped = false
                continue
            }
            if char == "\\" && inDouble {
                escaped = true
                continue
            }
            if char == "'" && !inDouble {
                inSingle.toggle()
                continue
            }
            if char == "\"" && !inSingle {
                inDouble.toggle()
                continue
            }
            if char == "#" && !inSingle && !inDouble {
                if index == value.startIndex || value[value.index(before: index)].isWhitespace {
                    return String(value[..<index]).trimmingCharacters(in: .whitespaces)
                }
            }
        }
        return value.trimmingCharacters(in: .whitespaces)
    }

    private static func inlineComment(in line: String) -> String {
        guard let equalsIndex = line.firstIndex(of: "=") else { return "" }
        let valueStart = line.index(after: equalsIndex)
        let value = line[valueStart...]
        var inSingle = false
        var inDouble = false
        var escaped = false

        for index in value.indices {
            let char = value[index]
            if escaped {
                escaped = false
                continue
            }
            if char == "\\" && inDouble {
                escaped = true
                continue
            }
            if char == "'" && !inDouble {
                inSingle.toggle()
                continue
            }
            if char == "\"" && !inSingle {
                inDouble.toggle()
                continue
            }
            if char == "#" && !inSingle && !inDouble {
                if index == value.startIndex || value[value.index(before: index)].isWhitespace {
                    return String(value[index...]).trimmingCharacters(in: .whitespaces)
                }
            }
        }
        return ""
    }

    private static func unquote(_ value: String) -> String {
        guard value.count >= 2 else {
            return value
        }
        if value.hasPrefix("\"") && value.hasSuffix("\"") {
            let inner = String(value.dropFirst().dropLast())
            return inner
                .replacingOccurrences(of: "\\\"", with: "\"")
                .replacingOccurrences(of: "\\\\", with: "\\")
        }
        if value.hasPrefix("'") && value.hasSuffix("'") {
            return String(value.dropFirst().dropLast())
        }
        return value
    }

    private static func formatEnvValue(_ rawValue: String) -> String {
        let value = rawValue
            .replacingOccurrences(of: "\n", with: " ")
            .trimmingCharacters(in: .whitespacesAndNewlines)
        let allowed = CharacterSet(charactersIn: "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_./:~,+-=@")
        if !value.isEmpty && value.unicodeScalars.allSatisfy({ allowed.contains($0) }) {
            return value
        }
        let escaped = value
            .replacingOccurrences(of: "\\", with: "\\\\")
            .replacingOccurrences(of: "\"", with: "\\\"")
        return "\"\(escaped)\""
    }

    private static func boolValue(_ value: String) -> Bool? {
        switch value.trimmingCharacters(in: .whitespacesAndNewlines).lowercased() {
        case "true", "1", "yes", "on":
            return true
        case "false", "0", "no", "off":
            return false
        default:
            return nil
        }
    }

    private static func isBoolValue(_ value: String) -> Bool {
        boolValue(value) != nil
    }

    private static func normalizedValue(_ value: String, for definition: EnvSettingDefinition) -> String {
        if (
            definition.key == "THREADKEEPER_SPAWN__DEFAULT"
            || definition.key.hasPrefix("THREADKEEPER_SPAWN__LOOP__")
        ), value.trimmingCharacters(in: .whitespacesAndNewlines).lowercased() == "agy" {
            return "antigravity"
        }
        guard case .toggle = definition.kind else {
            return value
        }
        switch boolValue(value) {
        case .some(true):
            return "true"
        case .some(false):
            return "false"
        case .none:
            return value
        }
    }
}

extension EnvSettingsStore: @unchecked Sendable {}

@MainActor
final class AgentStatusStore: ObservableObject {
    @Published var snapshot = AgentStatusSnapshot(
        generatedAt: 0,
        runningCount: 0,
        totalRssMb: 0,
        enabledLoopCount: 0,
        runningLoopCount: 0,
        readyLoopCount: 0,
        loops: [],
        recentResults: [],
        recentFailures: [],
        agents: []
    )
    @Published var lastError: String?
    @Published var isRefreshing = false
    @Published var isCleaningMemory = false
    @Published var isThreadKeeperDisabled = false
    @Published var isTogglingThreadKeeper = false

    private var timer: Timer?
    private var envSettingsWindowController: EnvSettingsWindowController?
    private var refreshInFlight = false
    private var toggleStateRefreshInFlight = false
    private var didPrimeResults = false
    private var didRequestSelfRestart = false
    private var seenResultIds: Set<String> = Set(
        UserDefaults.standard.stringArray(forKey: "seenResultIds") ?? []
    )

    func start() {
        guard timer == nil else {
            return
        }
        requestNotificationPermission()
        refreshThreadKeeperToggleState()
        refresh()
        timer = Timer.scheduledTimer(withTimeInterval: statusPollInterval, repeats: true) { [weak self] _ in
            Task { @MainActor in
                self?.refresh()
            }
        }
    }

    func refresh() {
        guard !refreshInFlight else {
            return
        }
        refreshInFlight = true
        isRefreshing = true
        let command = statusCommand()

        Task.detached(priority: .utility) {
            let result = Result {
                try autoreleasepool {
                    let data = try Self.runStatusCommand(
                        command: command,
                        arguments: ["--json"]
                    )
                    return try JSONDecoder().decode(AgentStatusSnapshot.self, from: data)
                }
            }

            await MainActor.run {
                self.refreshInFlight = false
                self.isRefreshing = false
                switch result {
                case .success(let newSnapshot):
                    self.handleUsefulResults(
                        newSnapshot.recentResults + (newSnapshot.recentFailures ?? [])
                    )
                    self.snapshot = newSnapshot
                    self.lastError = nil
                    self.refreshThreadKeeperToggleState()
                    self.checkAppMemoryPressure(reason: "poll")
                case .failure(let error):
                    self.lastError = error.localizedDescription
                }
            }
        }
    }

    func cleanMemory() {
        guard !isCleaningMemory else {
            return
        }
        isCleaningMemory = true
        let command = statusCommand()

        Task.detached(priority: .utility) {
            let result = Result {
                try autoreleasepool {
                    try Self.runStatusCommand(
                        command: command,
                        arguments: ["--cleanup-memory"]
                    )
                }
            }

            await MainActor.run {
                self.isCleaningMemory = false
                switch result {
                case .success:
                    self.refresh()
                    self.checkAppMemoryPressure(reason: "manual-cleanup")
                case .failure(let error):
                    self.lastError = error.localizedDescription
                }
            }
        }
    }

    func refreshThreadKeeperToggleState() {
        guard !toggleStateRefreshInFlight else {
            return
        }
        toggleStateRefreshInFlight = true
        let envFileURL = threadKeeperEnvFileURL()

        Task.detached(priority: .utility) {
            let disabled = Self.readThreadKeeperDisabled(envFileURL: envFileURL)

            await MainActor.run {
                self.isThreadKeeperDisabled = disabled
                self.toggleStateRefreshInFlight = false
            }
        }
    }

    func toggleThreadKeeper() {
        guard !isTogglingThreadKeeper else {
            return
        }
        isTogglingThreadKeeper = true
        let targetDisabled = !isThreadKeeperDisabled
        let envFileURL = threadKeeperEnvFileURL()

        Task.detached(priority: .utility) {
            let result = Result {
                try Self.setThreadKeeperDisabled(
                    targetDisabled,
                    envFileURL: envFileURL
                )
            }

            await MainActor.run {
                self.isTogglingThreadKeeper = false
                switch result {
                case .success:
                    self.isThreadKeeperDisabled = targetDisabled
                    self.lastError = nil
                case .failure(let error):
                    self.lastError = error.localizedDescription
                    self.refreshThreadKeeperToggleState()
                }
                self.refresh()
            }
        }
    }

    func openEnvSettings() {
        if envSettingsWindowController == nil {
            envSettingsWindowController = EnvSettingsWindowController(agentStore: self)
        }
        envSettingsWindowController?.present()
    }

    private func requestNotificationPermission() {
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound]) { _, _ in
        }
    }

    private func handleUsefulResults(_ results: [UsefulResult]) {
        let currentIds = Set(results.map(\.id))
        if !didPrimeResults {
            seenResultIds.formUnion(currentIds)
            didPrimeResults = true
            saveSeenResultIds()
            return
        }

        var changed = false
        for result in results.reversed() where !seenResultIds.contains(result.id) {
            // Gate delivery on the per-category toggle, but always mark seen so
            // enabling a toggle later never replays historical backlog.
            if result.shouldNotify {
                postNotification(for: result)
            }
            seenResultIds.insert(result.id)
            changed = true
        }
        if changed {
            saveSeenResultIds()
        }
    }

    private func saveSeenResultIds() {
        let capped = Array(seenResultIds.suffix(200))
        seenResultIds = Set(capped)
        UserDefaults.standard.set(capped, forKey: "seenResultIds")
    }

    private func postNotification(for result: UsefulResult) {
        let content = UNMutableNotificationContent()
        content.title = result.title
        content.subtitle = result.loopName
        content.body = result.summary
        content.sound = .default

        let request = UNNotificationRequest(
            identifier: "threadkeeper.\(result.id)",
            content: content,
            trigger: nil
        )
        UNUserNotificationCenter.current().add(request)
    }

    nonisolated func runStatusCommand(arguments: [String]) throws -> Data {
        try Self.runStatusCommand(command: statusCommand(), arguments: arguments)
    }

    nonisolated private static func runStatusCommand(
        command: (executable: String, arguments: [String]),
        arguments: [String],
        timeout: TimeInterval? = nil
    ) throws -> Data {
        let process = Process()
        let pipe = Pipe()
        let errPipe = Pipe()

        process.executableURL = URL(fileURLWithPath: command.executable)
        process.arguments = command.arguments + arguments
        process.standardInput = FileHandle.nullDevice
        process.standardOutput = pipe
        process.standardError = errPipe
        defer {
            pipe.fileHandleForReading.closeFile()
            errPipe.fileHandleForReading.closeFile()
        }

        try process.run()
        if let timeout {
            let finished = waitForExit(process, timeout: timeout)
            if !finished {
                process.terminate()
                _ = waitForExit(process, timeout: 2.0)
                if process.isRunning {
                    process.interrupt()
                }
                throw NSError(
                    domain: "ThreadKeeperAgentStatus",
                    code: 124,
                    userInfo: [
                        NSLocalizedDescriptionKey: "tk-agent-status timed out waiting for exit."
                    ]
                )
            }
        } else {
            process.waitUntilExit()
        }

        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        if process.terminationStatus == 0 {
            return data
        }
        let err = String(
            data: errPipe.fileHandleForReading.readDataToEndOfFile(),
            encoding: .utf8
        ) ?? "tk-agent-status failed"
        throw NSError(
            domain: "ThreadKeeperAgentStatus",
            code: Int(process.terminationStatus),
            userInfo: [NSLocalizedDescriptionKey: err.trimmingCharacters(in: .whitespacesAndNewlines)]
        )
    }

    nonisolated private static func waitForExit(_ process: Process, timeout: TimeInterval) -> Bool {
        let semaphore = DispatchSemaphore(value: 0)
        process.terminationHandler = { _ in
            semaphore.signal()
        }
        if !process.isRunning {
            process.terminationHandler = nil
            return true
        }
        let result = semaphore.wait(timeout: .now() + timeout)
        process.terminationHandler = nil
        return result == .success
    }

    nonisolated private static func readThreadKeeperDisabled(envFileURL: URL) -> Bool {
        guard let text = try? String(contentsOf: envFileURL, encoding: .utf8) else {
            return false
        }
        for line in text.components(separatedBy: .newlines) {
            guard let parsed = parseEnvAssignment(line),
                  !parsed.isCommented,
                  parsed.key == disableBackgroundDaemonsKey else {
                continue
            }
            return parseBool(parsed.value) ?? false
        }
        return false
    }

    nonisolated private static func setThreadKeeperDisabled(
        _ disabled: Bool,
        envFileURL: URL
    ) throws {
        let raw = (
            try? String(contentsOf: envFileURL, encoding: .utf8)
        ) ?? ""
        let updated = mergeThreadKeeperDisabled(raw: raw, disabled: disabled)
        try FileManager.default.createDirectory(
            at: envFileURL.deletingLastPathComponent(),
            withIntermediateDirectories: true
        )
        try updated.write(to: envFileURL, atomically: true, encoding: .utf8)

        try requestThreadKeeperRestart(timeout: 5.0)
    }

    nonisolated private static func requestThreadKeeperRestart(timeout: TimeInterval) throws {
        let process = Process()
        let errPipe = Pipe()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/pkill")
        process.arguments = ["-TERM", "-f", "threadkeeper.server"]
        process.standardInput = FileHandle.nullDevice
        process.standardOutput = FileHandle.nullDevice
        process.standardError = errPipe
        defer {
            errPipe.fileHandleForReading.closeFile()
        }

        try process.run()
        let finished = waitForExit(process, timeout: timeout)
        if !finished {
            process.terminate()
            _ = waitForExit(process, timeout: 1.0)
            throw NSError(
                domain: "ThreadKeeperAgentStatus",
                code: 124,
                userInfo: [
                    NSLocalizedDescriptionKey: "ThreadKeeper restart request timed out."
                ]
            )
        }
        if process.terminationStatus == 0 || process.terminationStatus == 1 {
            return
        }

        let err = String(
            data: errPipe.fileHandleForReading.readDataToEndOfFile(),
            encoding: .utf8
        ) ?? "pkill failed"
        throw NSError(
            domain: "ThreadKeeperAgentStatus",
            code: Int(process.terminationStatus),
            userInfo: [NSLocalizedDescriptionKey: err.trimmingCharacters(in: .whitespacesAndNewlines)]
        )
    }

    nonisolated private static func mergeThreadKeeperDisabled(raw: String, disabled: Bool) -> String {
        let value = disabled ? "true" : "false"
        var output: [String] = []
        var seen = false

        for line in raw.components(separatedBy: .newlines) {
            guard let parsed = parseEnvAssignment(line),
                  parsed.key == disableBackgroundDaemonsKey else {
                output.append(line)
                continue
            }

            if !seen {
                output.append("\(disableBackgroundDaemonsKey)=\(value)")
                seen = true
            } else {
                output.append(parsed.isCommented ? line : "# \(line)")
            }
        }

        if !seen {
            if !output.isEmpty && output.last != "" {
                output.append("")
            }
            output.append("# Updated by ThreadKeeper widget")
            output.append("\(disableBackgroundDaemonsKey)=\(value)")
        }

        while output.last == "" {
            output.removeLast()
        }
        return output.isEmpty ? "" : output.joined(separator: "\n") + "\n"
    }

    nonisolated private static func parseEnvAssignment(
        _ line: String
    ) -> (key: String, value: String, isCommented: Bool)? {
        var trimmed = line.trimmingCharacters(in: .whitespaces)
        var isCommented = false
        if trimmed.hasPrefix("#") {
            isCommented = true
            trimmed = String(trimmed.dropFirst()).trimmingCharacters(in: .whitespaces)
        }
        guard let equalsIndex = trimmed.firstIndex(of: "=") else {
            return nil
        }
        let key = String(trimmed[..<equalsIndex]).trimmingCharacters(in: .whitespaces)
        guard key == disableBackgroundDaemonsKey else {
            return nil
        }
        let valueStart = trimmed.index(after: equalsIndex)
        let value = String(trimmed[valueStart...])
            .split(separator: "#", maxSplits: 1, omittingEmptySubsequences: false)[0]
            .trimmingCharacters(in: .whitespacesAndNewlines)
            .trimmingCharacters(in: CharacterSet(charactersIn: "\"'"))
        return (key, value, isCommented)
    }

    nonisolated private static func parseBool(_ value: String) -> Bool? {
        switch value.trimmingCharacters(in: .whitespacesAndNewlines).lowercased() {
        case "true", "1", "yes", "on":
            return true
        case "false", "0", "no", "off":
            return false
        default:
            return nil
        }
    }

    nonisolated private func statusCommand() -> (executable: String, arguments: [String]) {
        let env = ProcessInfo.processInfo.environment
        if let override = env["THREADKEEPER_AGENT_STATUS_COMMAND"], !override.isEmpty {
            return (override, [])
        }

        let home = FileManager.default.homeDirectoryForCurrentUser.path
        let candidates = [
            "/opt/homebrew/bin/tk-agent-status",
            "/usr/local/bin/tk-agent-status",
            "\(home)/.local/bin/tk-agent-status",
        ]
        for path in candidates where FileManager.default.isExecutableFile(atPath: path) {
            return (path, [])
        }

        return ("/usr/bin/env", ["tk-agent-status"])
    }

    private var memoryRestartThresholdMB: Int {
        let env = ProcessInfo.processInfo.environment
        guard let raw = env["THREADKEEPER_MENUBAR_RESTART_RSS_MB"],
              let value = Int(raw) else {
            return 1024
        }
        return max(0, value)
    }

    private func checkAppMemoryPressure(reason: String) {
        let threshold = memoryRestartThresholdMB
        guard threshold > 0, !didRequestSelfRestart else {
            return
        }
        let rssMb = currentRSSMB()
        guard rssMb >= threshold else {
            return
        }
        didRequestSelfRestart = true
        restartSelf(reason: reason, rssMb: rssMb, thresholdMb: threshold)
    }

    private func currentRSSMB() -> Int {
        let process = Process()
        let pipe = Pipe()
        process.executableURL = URL(fileURLWithPath: "/bin/ps")
        process.arguments = [
            "-o",
            "rss=",
            "-p",
            String(ProcessInfo.processInfo.processIdentifier),
        ]
        process.standardInput = FileHandle.nullDevice
        process.standardOutput = pipe
        process.standardError = FileHandle.nullDevice
        defer {
            pipe.fileHandleForReading.closeFile()
        }
        do {
            try process.run()
            process.waitUntilExit()
        } catch {
            return 0
        }
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        guard process.terminationStatus == 0,
              let text = String(data: data, encoding: .utf8),
              let rssKb = Int(text.trimmingCharacters(in: .whitespacesAndNewlines)) else {
            return 0
        }
        return rssKb / 1024
    }

    private func restartSelf(reason: String, rssMb: Int, thresholdMb: Int) {
        do {
            _ = try? runStatusCommand(arguments: ["--cleanup-memory"])
            let process = Process()
            process.executableURL = URL(fileURLWithPath: "/usr/bin/open")
            process.arguments = ["-n", Bundle.main.bundlePath]
            try process.run()
        } catch {
            lastError = (
                "memory cleanup restart failed "
                + "rss=\(rssMb)MB threshold=\(thresholdMb)MB "
                + "reason=\(reason): \(error.localizedDescription)"
            )
            didRequestSelfRestart = false
            return
        }
        NSApplication.shared.terminate(nil)
    }
}

struct LoopRow: View {
    let loop: LoopStatus

    private var statusColor: Color {
        switch loop.status {
        case "running":
            return .green
        case "ready":
            return .blue
        case "idle":
            return .secondary
        default:
            return .gray
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                Circle()
                    .fill(statusColor)
                    .frame(width: 8, height: 8)
                Text(loop.name)
                    .font(.system(size: 13, weight: .semibold, design: .rounded))
                    .foregroundStyle(.primary)
                    .lineLimit(1)
                Spacer()
                StatusPill(status: loop.status)
            }
            Text(loop.description)
                .font(.system(size: 12, weight: .regular))
                .lineLimit(3)
                .foregroundStyle(loop.status == "off" ? .tertiary : .primary)
            Text(loop.work)
                .font(.system(size: 11, weight: .medium))
                .lineLimit(2)
                .foregroundStyle(loop.status == "off" ? .tertiary : .secondary)
            HStack(spacing: 10) {
                MetricLabel(systemImage: "clock", text: loop.lastAge == "never" ? "never" : "\(loop.lastAge) ago")
                if !loop.backlogLabel.isEmpty {
                    MetricLabel(systemImage: "tray", text: "\(loop.backlogCount)")
                }
                if loop.runningAgentCount > 0 {
                    MetricLabel(systemImage: "person.crop.circle.badge.checkmark", text: "\(loop.runningAgentCount)")
                }
                Spacer()
                MetricLabel(systemImage: "memorychip", text: "\(loop.rssMb) MB")
            }
        }
        .padding(10)
        .background(rowBackground, in: RoundedRectangle(cornerRadius: 8))
        .frame(width: panelWidth - 24, alignment: .leading)
    }

    private var rowBackground: Color {
        loop.status == "off"
            ? Color(nsColor: .controlBackgroundColor).opacity(0.45)
            : Color(nsColor: .controlBackgroundColor)
    }
}

struct StatusPill: View {
    let status: String

    var body: some View {
        Text(status)
            .font(.system(size: 10, weight: .semibold, design: .rounded))
            .foregroundStyle(status == "off" ? .secondary : .primary)
            .padding(.horizontal, 7)
            .padding(.vertical, 3)
            .background(Color(nsColor: .windowBackgroundColor), in: Capsule())
    }
}

struct MetricLabel: View {
    let systemImage: String
    let text: String

    var body: some View {
        Label(text, systemImage: systemImage)
            .font(.system(size: 11, weight: .medium))
            .labelStyle(.titleAndIcon)
            .foregroundStyle(.secondary)
    }
}

struct EmptyState: View {
    var body: some View {
        VStack(spacing: 8) {
            Image(systemName: "gearshape.2")
                .font(.system(size: 26, weight: .regular))
                .foregroundStyle(.secondary)
            Text("No loop data yet")
                .font(.system(size: 13, weight: .semibold))
                .foregroundStyle(.primary)
        }
        .frame(width: panelWidth - 24)
        .frame(minHeight: 112)
        .background(Color(nsColor: .controlBackgroundColor), in: RoundedRectangle(cornerRadius: 8))
    }
}

struct ErrorState: View {
    let message: String

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: "exclamationmark.triangle.fill")
                .foregroundStyle(.orange)
            Text(message.isEmpty ? "tk-agent-status failed" : message)
                .font(.system(size: 12))
                .foregroundStyle(.primary)
                .lineLimit(4)
            Spacer(minLength: 0)
        }
        .padding(10)
        .frame(width: panelWidth - 24, alignment: .leading)
        .background(Color.orange.opacity(0.12), in: RoundedRectangle(cornerRadius: 8))
    }
}

struct ThreadKeeperToggleBar: View {
    @EnvironmentObject var store: AgentStatusStore

    private var isOff: Bool {
        store.isThreadKeeperDisabled
    }

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: isOff ? "pause.circle.fill" : "checkmark.circle.fill")
                .font(.system(size: 17, weight: .semibold))
                .foregroundStyle(isOff ? .orange : .green)
                .frame(width: 22)
            VStack(alignment: .leading, spacing: 2) {
                Text(isOff ? "ThreadKeeper background is off" : "ThreadKeeper background is on")
                    .font(.system(size: 12, weight: .semibold, design: .rounded))
                Text(isOff ? "Autonomous loops are paused." : "Autonomous loops can run.")
                    .font(.system(size: 11, weight: .medium))
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }
            Spacer(minLength: 8)
            Button {
                store.toggleThreadKeeper()
            } label: {
                Label(store.isThreadKeeperDisabled ? "Turn On" : "Turn Off", systemImage: "power")
                    .labelStyle(.titleAndIcon)
            }
            .controlSize(.small)
            .buttonStyle(.borderedProminent)
            .tint(isOff ? .green : .red)
            .disabled(store.isTogglingThreadKeeper)
            .help(isOff ? "Enable ThreadKeeper" : "Disable ThreadKeeper")
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 9)
        .frame(width: panelWidth - 24, alignment: .leading)
        .background(Color(nsColor: .controlBackgroundColor), in: RoundedRectangle(cornerRadius: 8))
    }
}

@MainActor
final class EnvSettingsWindowController: NSWindowController {
    private let envStore: EnvSettingsStore

    init(agentStore: AgentStatusStore) {
        envStore = EnvSettingsStore(agentStore: agentStore)
        let hostingController = NSHostingController(
            rootView: EnvSettingsView(envStore: envStore)
        )
        let window = NSWindow(
            contentRect: NSRect(
                x: 0,
                y: 0,
                width: settingsWindowWidth,
                height: settingsWindowHeight
            ),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        window.title = "ThreadKeeper Settings"
        window.contentViewController = hostingController
        window.setFrameAutosaveName("ThreadKeeperEnvSettingsWindow")
        window.center()
        super.init(window: window)
        self.window?.isReleasedWhenClosed = false
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    func present() {
        let shouldReload = window?.isVisible != true
        showWindow(nil)
        window?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        if shouldReload {
            DispatchQueue.main.async { [envStore] in
                envStore.reloadOnWindowPresentation()
            }
        }
    }
}

struct EnvSettingsView: View {
    @ObservedObject var envStore: EnvSettingsStore
    @State private var selectedSection: EnvSettingsSection? = .cliAgents
    private let agentColumns = [
        GridItem(.adaptive(minimum: 340, maximum: .infinity), spacing: 14, alignment: .top)
    ]
    private let automationColumns = [
        GridItem(.adaptive(minimum: 340, maximum: .infinity), spacing: 14, alignment: .top)
    ]

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            HStack(spacing: 0) {
                List(EnvSettingsSection.allCases, selection: sectionSelection) { section in
                    Label(section.rawValue, systemImage: section.icon)
                        .tag(section)
                }
                .listStyle(.sidebar)
                .frame(width: 205)
                Divider()
                sectionContent
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
            Divider()
            footer
        }
        .frame(minWidth: 900, minHeight: 650)
    }

    private var header: some View {
        HStack(spacing: 10) {
            Image(systemName: "gearshape")
                .font(.system(size: 20, weight: .semibold))
                .foregroundStyle(.blue)
            VStack(alignment: .leading, spacing: 2) {
                Text("ThreadKeeper Settings")
                    .font(.system(size: 16, weight: .semibold, design: .rounded))
                Text(envStore.envPath)
                    .font(.system(size: 11, weight: .medium, design: .monospaced))
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .truncationMode(.middle)
            }
            Spacer()
            PresetMenu(envStore: envStore)
            Button {
                envStore.reloadEnvWithConfirmation()
            } label: {
                Image(systemName: "arrow.clockwise")
                    .font(.system(size: 13, weight: .semibold))
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Reload .env")
            .help("Reload .env")
            Button {
                envStore.refreshCatalog(force: true)
            } label: {
                Image(systemName: "arrow.triangle.2.circlepath")
                    .font(.system(size: 13, weight: .semibold))
            }
            .buttonStyle(.plain)
            .disabled(envStore.isCatalogLoading)
            .accessibilityLabel("Refresh CLI models and capabilities")
            .help("Refresh CLI models and capabilities")
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 12)
    }

    @ViewBuilder
    private var sectionContent: some View {
        switch selectedSection ?? .cliAgents {
        case .cliAgents: cliAgentsSection
        case .learningAgents: learningAgentsSection
        case .automation: automationSection
        case .memory: memorySection
        case .notifications: notificationsSection
        case .advanced: advancedSection
        }
    }

    private var cliAgentsSection: some View {
        SettingsScroll(title: "CLI Agents", subtitle: "Live capabilities from each installed agent CLI.") {
            if !runtimeOverrideWarnings.isEmpty {
                SettingsWarningBox(messages: runtimeOverrideWarnings)
            }
            if let definition = envStore.definition(for: "THREADKEEPER_SPAWN__DEFAULT") {
                EnvSettingSection(title: "Default routing", definitions: [definition], envStore: envStore)
            }
            catalogState
            LazyVGrid(columns: agentColumns, alignment: .leading, spacing: 14) {
                ForEach(envStore.catalog?.clis ?? []) { cli in
                    CLISettingsCard(cli: cli, envStore: envStore)
                }
            }
        }
    }

    private var learningAgentsSection: some View {
        SettingsScroll(
            title: "Learning Loop Agents",
            subtitle: "Each card is an actual LLM-backed role. Model and effort inherit from its selected CLI unless overridden."
        ) {
            if !runtimeOverrideWarnings.isEmpty {
                SettingsWarningBox(messages: runtimeOverrideWarnings)
            }
            catalogState
            LazyVGrid(columns: agentColumns, alignment: .leading, spacing: 14) {
                ForEach(envStore.catalog?.agentRoles ?? []) { role in
                    LearningAgentCard(role: role, envStore: envStore)
                }
            }
        }
    }

    private var automationSection: some View {
        SettingsScroll(
            title: "System Automation",
            subtitle: "Deterministic background jobs. These jobs do not use an LLM model or reasoning effort."
        ) {
            catalogState
            LazyVGrid(columns: automationColumns, alignment: .leading, spacing: 14) {
                ForEach(envStore.catalog?.mechanicalJobs ?? []) { job in
                    AutomationJobCard(job: job, envStore: envStore)
                }
            }
            let mechanicalKeys = Set(
                (envStore.catalog?.mechanicalJobs ?? []).map(\.intervalKey)
            )
            let core = baseEnvSettingDefinitions.filter {
                $0.group == "Core"
                    && !mechanicalKeys.contains($0.key)
                    && $0.key != "THREADKEEPER_NO_EMBEDDINGS"
            }
            if !core.isEmpty {
                EnvSettingSection(title: "Runtime", definitions: core, envStore: envStore)
            }
        }
    }

    private var memorySection: some View {
        SettingsScroll(
            title: "Memory & Budgets",
            subtitle: "Search behavior, memory pressure thresholds, and child-agent budgets."
        ) {
            LazyVGrid(columns: automationColumns, alignment: .leading, spacing: 14) {
                EnvSettingSection(
                    title: "Search",
                    definitions: baseEnvSettingDefinitions.filter {
                        $0.key == "THREADKEEPER_NO_EMBEDDINGS"
                    },
                    envStore: envStore
                )
                EnvSettingSection(
                    title: "Memory and resource limits",
                    definitions: baseEnvSettingDefinitions.filter {
                        $0.group == "Memory And Budgets"
                    },
                    envStore: envStore
                )
            }
        }
    }

    private var notificationsSection: some View {
        SettingsScroll(
            title: "Notifications",
            subtitle: "Native macOS banners when a background loop can't run — or when a skill/lesson is captured. All off until you pick an interval."
        ) {
            LazyVGrid(columns: automationColumns, alignment: .leading, spacing: 14) {
                EnvSettingSection(
                    title: "Delivery",
                    definitions: baseEnvSettingDefinitions.filter {
                        $0.group == "Notifications"
                            && ($0.key == "THREADKEEPER_NOTIFY_POLL_S"
                                || $0.key == "THREADKEEPER_NOTIFY_CHANNEL"
                                || $0.key == "THREADKEEPER_NOTIFY_FAILURE_COOLDOWN_S")
                    },
                    envStore: envStore
                )
                EnvSettingSection(
                    title: "What to notify",
                    definitions: baseEnvSettingDefinitions.filter {
                        $0.group == "Notifications"
                            && ($0.key == "THREADKEEPER_NOTIFY_LOOP_FAILURE"
                                || $0.key == "THREADKEEPER_NOTIFY_SKILL_MATERIALIZED"
                                || $0.key == "THREADKEEPER_NOTIFY_LESSON")
                    },
                    envStore: envStore
                )
            }
        }
    }

    private var advancedSection: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 8) {
                VStack(alignment: .leading, spacing: 2) {
                    Text("Advanced .env")
                        .font(.system(size: 18, weight: .semibold, design: .rounded))
                    Text("Unknown keys, comments, ordering, and duplicate assignments are preserved.")
                        .font(.system(size: 12))
                        .foregroundStyle(.secondary)
                }
                Spacer()
                Button("Import into form") { envStore.importRawIntoForm() }
                Button("Preview form changes") { envStore.syncFormToRaw() }
            }
            if !envStore.configurationWarnings.isEmpty {
                SettingsWarningBox(messages: envStore.configurationWarnings)
            }
            TextEditor(text: envStore.rawDraftBinding())
                .font(.system(size: 12, weight: .regular, design: .monospaced))
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .overlay(
                    RoundedRectangle(cornerRadius: 6)
                        .stroke(Color(nsColor: .separatorColor).opacity(0.6))
                )
        }
        .padding(18)
    }

    @ViewBuilder
    private var catalogState: some View {
        if envStore.isCatalogLoading && envStore.catalog == nil {
            HStack(spacing: 8) {
                ProgressView().controlSize(.small)
                Text("Reading installed CLIs and current model catalogs…")
                    .font(.system(size: 12))
                    .foregroundStyle(.secondary)
            }
            .padding(10)
        } else if !envStore.catalogError.isEmpty {
            SettingsWarningBox(messages: [envStore.catalogError])
        }
    }

    private var footer: some View {
        HStack(spacing: 10) {
            Image(systemName: footerIcon)
                .foregroundStyle(footerColor)
            Text(footerMessage)
                .font(.system(size: 11, weight: .medium))
                .foregroundStyle(envStore.validationMessages.isEmpty ? .secondary : .primary)
                .lineLimit(2)
            Spacer()
            Button {
                save(restart: true)
            } label: {
                Label("Save & Restart", systemImage: "arrow.clockwise")
            }
            .buttonStyle(.borderless)
            .disabled(!envStore.canSave)
            Button {
                save(restart: false)
            } label: {
                Label("Save Changes", systemImage: "square.and.arrow.down")
            }
            .keyboardShortcut(.defaultAction)
            .disabled(!envStore.canSave)
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 12)
    }

    private func save(restart: Bool) {
        envStore.saveDraft(
            editingAdvanced: selectedSection == .advanced,
            restart: restart
        )
    }

    private var sectionSelection: Binding<EnvSettingsSection?> {
        Binding(
            get: { selectedSection },
            set: { next in
                let previous = selectedSection ?? .cliAgents
                if next != selectedSection {
                    envStore.reconcileDraft(leavingAdvanced: previous == .advanced)
                }
                selectedSection = next
            }
        )
    }

    private var footerMessage: String {
        if let first = envStore.validationMessages.first {
            let remaining = envStore.validationMessages.count - 1
            return remaining > 0 ? "\(first) +\(remaining) more" : first
        }
        if let first = envStore.configurationWarnings.first {
            let remaining = envStore.configurationWarnings.count - 1
            return remaining > 0 ? "\(first) +\(remaining) more" : first
        }
        return envStore.statusMessage
    }

    private var footerIcon: String {
        if !envStore.validationMessages.isEmpty { return "xmark.octagon.fill" }
        if !envStore.configurationWarnings.isEmpty { return "exclamationmark.triangle.fill" }
        return "checkmark.circle"
    }

    private var footerColor: Color {
        if !envStore.validationMessages.isEmpty { return .red }
        if !envStore.configurationWarnings.isEmpty { return .orange }
        return .green
    }

    private var runtimeOverrideWarnings: [String] {
        (envStore.catalog?.runtimeOverrides ?? []).map { override in
            "Process environment overrides .env: \(override.key)=\(override.value)"
        }
    }
}

struct SettingsScroll<Content: View>: View {
    let title: String
    let subtitle: String
    @ViewBuilder let content: Content

    init(title: String, subtitle: String, @ViewBuilder content: () -> Content) {
        self.title = title
        self.subtitle = subtitle
        self.content = content()
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                VStack(alignment: .leading, spacing: 3) {
                    Text(title).font(.system(size: 18, weight: .semibold, design: .rounded))
                    Text(subtitle).font(.system(size: 12)).foregroundStyle(.secondary)
                }
                content
            }
            .padding(18)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .background(Color(nsColor: .windowBackgroundColor).opacity(0.45))
    }
}

private extension View {
    func settingsCardSurface() -> some View {
        self
            .padding(14)
            .background(
                Color(nsColor: .controlBackgroundColor),
                in: RoundedRectangle(cornerRadius: 10, style: .continuous)
            )
            .overlay(
                RoundedRectangle(cornerRadius: 10, style: .continuous)
                    .stroke(Color(nsColor: .separatorColor).opacity(0.35), lineWidth: 1)
            )
    }
}

struct PresetMenu: View {
    @ObservedObject var envStore: EnvSettingsStore

    var body: some View {
        Menu {
            ForEach(envStore.presetSlots) { preset in
                Menu(preset.name) {
                    Button("Load") { envStore.loadPresetWithConfirmation(slot: preset.slot) }
                        .disabled(preset.rawEnvText.isEmpty && preset.values.isEmpty)
                    Button("Update from current") { envStore.savePreset(slot: preset.slot) }
                    Button("Clear", role: .destructive) { envStore.clearPreset(slot: preset.slot) }
                        .disabled(preset.rawEnvText.isEmpty && preset.values.isEmpty)
                }
            }
        } label: {
            Label("Presets", systemImage: "square.grid.3x1.folder.badge.plus")
        }
        .menuStyle(.borderlessButton)
        .accessibilityLabel("Settings presets")
        .help("Load or update a settings preset")
    }
}

struct CLISettingsCard: View {
    let cli: CLICatalogEntry
    @ObservedObject var envStore: EnvSettingsStore

    private var modelKey: String { "THREADKEEPER_SPAWN__MODEL__\(cli.id.uppercased())" }
    private var effortKey: String { "THREADKEEPER_SPAWN__EFFORT__\(cli.id.uppercased())" }
    private var latestVersion: String { cli.latestVersion ?? "" }
    private var isUpdating: Bool { envStore.updatingCLIID == cli.id }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .top, spacing: 10) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(cli.name).font(.system(size: 14, weight: .semibold))
                    Text(cli.version.isEmpty ? "Installed version unavailable" : "Installed: \(cli.version)")
                        .font(.system(size: 11, design: .monospaced))
                        .foregroundStyle(.secondary)
                    if !latestVersion.isEmpty {
                        Text("Latest cloud: \(latestVersion)")
                            .font(.system(size: 11, design: .monospaced))
                            .foregroundStyle(cli.updateAvailable == true ? Color.orange : Color.secondary)
                    }
                }
                Spacer()
                VStack(alignment: .trailing, spacing: 6) {
                    StatusBadge(
                        text: cli.installed ? "Installed" : "Not installed",
                        color: cli.installed ? .green : .secondary
                    )
                    if cli.updateAvailable == true {
                        Button {
                            envStore.updateCLI(cli)
                        } label: {
                            if isUpdating {
                                HStack(spacing: 5) {
                                    ProgressView().controlSize(.small)
                                    Text("Updating…")
                                }
                            } else {
                                Label("Update", systemImage: "arrow.down.circle")
                            }
                        }
                        .buttonStyle(.borderedProminent)
                        .controlSize(.small)
                        .disabled(isUpdating || cli.updateSupported != true)
                        .help(
                            cli.updateSupported == true
                                ? "Update \(cli.name) to \(latestVersion) with \(cli.updateCommandLabel ?? "its official updater")"
                                : "A newer version exists, but no supported updater was detected."
                        )
                        .accessibilityLabel("Update \(cli.name) to \(latestVersion)")
                    }
                }
            }
            if !cli.executable.isEmpty {
                Text(cli.executable)
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundStyle(.tertiary)
                    .lineLimit(1)
                    .truncationMode(.middle)
            }
            Divider()
            if let model = envStore.definition(for: modelKey) {
                EnvSettingRow(definition: model, envStore: envStore, showKey: false)
            }
            if cli.effortMode == "independent",
               let effort = envStore.definition(for: effortKey) {
                EnvSettingRow(definition: effort, envStore: envStore, showKey: false)
            } else if !cli.effortNote.isEmpty {
                Label(cli.effortNote, systemImage: "info.circle")
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
            }
            if let configuredEffort = envStore.values[effortKey],
               !configuredEffort.isEmpty,
               !envStore.effortOptions(
                    for: cli,
                    model: envStore.values[modelKey] ?? ""
               ).contains(configuredEffort) {
                Label(
                    "Effort \"\(configuredEffort)\" is kept, but the selected model does not advertise it.",
                    systemImage: "exclamationmark.triangle"
                )
                .font(.system(size: 11))
                .foregroundStyle(.orange)
            }
            HStack {
                Text("Models: \(cli.models.count) · \(cli.modelSource)")
                Spacer()
                Text(cli.catalogAgeS == 0 ? "just refreshed" : "refreshed \(humanInterval(Double(cli.catalogAgeS))) ago")
            }
            .font(.system(size: 10))
            .foregroundStyle(cli.stale ? Color.orange : Color.secondary)
            if let error = cli.error, !error.isEmpty {
                Label(error, systemImage: "exclamationmark.triangle")
                    .font(.system(size: 11))
                    .foregroundStyle(.orange)
            }
            if let versionError = cli.versionCheckError, !versionError.isEmpty {
                Label(versionError, systemImage: "icloud.slash")
                    .font(.system(size: 11))
                    .foregroundStyle(.orange)
            }
            if !cli.configuredModel.isEmpty && !cli.configuredModelInCatalog {
                Label("Configured model \"\(cli.configuredModel)\" is kept as a custom value but is not in the current catalog.", systemImage: "pin")
                    .font(.system(size: 11))
                    .foregroundStyle(.orange)
            }
        }
        .settingsCardSurface()
        .opacity(cli.installed ? 1 : 0.72)
        .accessibilityElement(children: .contain)
        .accessibilityLabel("\(cli.name) CLI settings")
    }
}

struct LearningAgentCard: View {
    let role: LearningAgentDefinition
    @ObservedObject var envStore: EnvSettingsStore

    private var token: String { role.role.uppercased() }
    private var roleModelKey: String { "THREADKEEPER_SPAWN__MODEL__\(token)" }
    private var draftCLI: String { envStore.draftCLI(for: role) }
    private var cli: CLICatalogEntry? { envStore.cli(for: draftCLI) }
    private var runtimeCLI: CLICatalogEntry? { envStore.cli(for: role.cli) }
    private var runtimeCLIText: String {
        role.cliDynamic || role.cli.isEmpty
            ? "Active host CLI (fallback Claude)"
            : role.cli
    }
    private var runtimeModelText: String { role.model.isEmpty ? "CLI default" : role.model }
    private var runtimeEffortText: String { role.effort.isEmpty ? "CLI default" : role.effort }
    private var draftCLIText: String {
        draftCLI.isEmpty ? "Active host CLI (fallback Claude)" : draftCLI
    }
    private var hasPendingPreview: Bool {
        let differs = draftCLIText != runtimeCLIText
            || envStore.draftModel(for: role) != runtimeModelText
            || (cli?.effortMode == "independent"
                && envStore.draftEffort(for: role) != runtimeEffortText)
        return differs && (envStore.isDraftDirty || hasProcessOverride)
    }
    private var hasProcessOverride: Bool {
        [role.cliSource, role.modelSource, role.effortSource]
            .contains("process environment")
            || envStore.hasRuntimeOverride(for: role)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .top) {
                VStack(alignment: .leading, spacing: 3) {
                    Text(role.name).font(.system(size: 14, weight: .semibold))
                    Text(role.description).font(.system(size: 12)).foregroundStyle(.secondary)
                }
                Spacer()
                ImpactBadge(impact: role.impact)
            }
            HStack(alignment: .top, spacing: 18) {
                ImpactLine(icon: "book", label: "Reads", value: role.reads)
                ImpactLine(icon: "square.and.pencil", label: "Writes", value: role.writes)
            }
            Divider()
            if let definition = envStore.definition(for: "THREADKEEPER_SPAWN__LOOP__\(token)") {
                EnvSettingRow(definition: definition, envStore: envStore, showKey: false)
            }
            if let definition = envStore.definition(for: "THREADKEEPER_SPAWN__MODEL__\(token)") {
                EnvSettingRow(definition: definition, envStore: envStore, showKey: false)
            }
            if cli?.effortMode == "independent",
               let definition = envStore.definition(for: "THREADKEEPER_SPAWN__EFFORT__\(token)") {
                EnvSettingRow(definition: definition, envStore: envStore, showKey: false)
            } else if let cli, !cli.effortNote.isEmpty {
                Label(cli.effortNote, systemImage: "slider.horizontal.below.square.filled.and.square")
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
            }
            if !role.intervalKey.isEmpty,
               let schedule = envStore.definition(for: role.intervalKey) {
                EnvSettingRow(definition: schedule, envStore: envStore, showKey: false)
                ScheduleHint(
                    value: envStore.values[role.intervalKey] ?? "",
                    defaultValue: schedule.defaultValue
                )
            } else {
                Label("Runs on demand; no autonomous schedule.", systemImage: "hand.tap")
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
            }
            HStack(spacing: 6) {
                Text("Current runtime:").foregroundStyle(.secondary)
                Text(runtimeCLIText).fontWeight(.semibold)
                Text("→").foregroundStyle(.tertiary)
                Text(runtimeModelText).fontWeight(.semibold)
                if runtimeCLI?.effortMode == "independent" {
                    Text("→").foregroundStyle(.tertiary)
                    Text(runtimeEffortText).fontWeight(.semibold)
                }
                Spacer()
                Text("\(role.cliSource) · \(role.modelSource) · \(role.effortSource)")
                    .foregroundStyle(.tertiary)
            }
            .font(.system(size: 10))
            if hasProcessOverride {
                Label(
                    "Overridden by process environment; saving .env will not take precedence until that override is removed.",
                    systemImage: "exclamationmark.shield"
                )
                .font(.system(size: 10))
                .foregroundStyle(.orange)
            }
            if let runtimeCLI,
               !role.effort.isEmpty,
               !envStore.effortOptions(
                    for: runtimeCLI,
                    model: role.model
               ).contains(role.effort) {
                Label(
                    "Current runtime effort \"\(role.effort)\" is not advertised by model \"\(runtimeModelText)\".",
                    systemImage: "exclamationmark.triangle"
                )
                .font(.system(size: 10))
                .foregroundStyle(.orange)
            }
            if hasPendingPreview {
                HStack(spacing: 6) {
                    Text(envStore.isDraftDirty ? "Pending draft preview:" : "File configuration:")
                        .foregroundStyle(.secondary)
                    Text(draftCLIText).fontWeight(.semibold)
                    Text("→").foregroundStyle(.tertiary)
                    Text(envStore.draftModel(for: role)).fontWeight(.semibold)
                    if cli?.effortMode == "independent" {
                        Text("→").foregroundStyle(.tertiary)
                        Text(envStore.draftEffort(for: role)).fontWeight(.semibold)
                    }
                }
                .font(.system(size: 10))
                .foregroundStyle(.blue)
            }
            if let cli,
               let selectedModel = envStore.values[roleModelKey],
               !selectedModel.isEmpty,
               !cli.models.contains(selectedModel) {
                Label(
                    "Model \"\(selectedModel)\" is kept as a custom value, but \(cli.name) does not advertise it. Verify provider compatibility before saving.",
                    systemImage: "exclamationmark.triangle"
                )
                .font(.system(size: 10))
                .foregroundStyle(.orange)
            }
            if let cli,
               let effort = envStore.values["THREADKEEPER_SPAWN__EFFORT__\(token)"],
               !effort.isEmpty,
               !envStore.effortOptions(
                    for: cli,
                    model: envStore.draftModel(for: role)
               ).contains(effort) {
                Label(
                    "Effort \"\(effort)\" is kept, but the selected model does not advertise it.",
                    systemImage: "exclamationmark.triangle"
                )
                .font(.system(size: 10))
                .foregroundStyle(.orange)
            }
        }
        .settingsCardSurface()
        .accessibilityElement(children: .contain)
        .accessibilityLabel("\(role.name) Learning Loop agent")
    }
}

struct AutomationJobCard: View {
    let job: MechanicalJobDefinition
    @ObservedObject var envStore: EnvSettingsStore

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                VStack(alignment: .leading, spacing: 3) {
                    Text(job.name).font(.system(size: 13, weight: .semibold))
                    Text(job.description).font(.system(size: 11)).foregroundStyle(.secondary)
                }
                Spacer()
                StatusBadge(text: "Mechanical", color: .secondary)
            }
            if let definition = envStore.definition(for: job.intervalKey) {
                Divider()
                AutomationScheduleRow(definition: definition, envStore: envStore)
                ScheduleHint(
                    value: envStore.values[job.intervalKey] ?? "",
                    defaultValue: definition.defaultValue
                )
            }
        }
        .settingsCardSurface()
    }
}

struct AutomationScheduleRow: View {
    let definition: EnvSettingDefinition
    @ObservedObject var envStore: EnvSettingsStore

    var body: some View {
        HStack(spacing: 8) {
            Text("Schedule (hours)")
                .font(.system(size: 11, weight: .semibold))
                .lineLimit(1)
                .layoutPriority(1)
            Spacer(minLength: 8)
            Picker("Schedule (hours)", selection: envStore.binding(for: definition.key)) {
                Text(inheritedDefaultLabel(for: definition)).tag("")
                ForEach(choicesIncludingCurrent, id: \.value) { choice in
                    Text(choice.label).tag(choice.value)
                }
            }
            .labelsHidden()
            .pickerStyle(.menu)
            .frame(minWidth: 135, idealWidth: 170, maxWidth: 190)
            Button {
                envStore.setValue("", for: definition.key)
            } label: {
                Image(systemName: "xmark.circle.fill")
                    .foregroundStyle(.tertiary)
            }
            .buttonStyle(.plain)
            .disabled((envStore.values[definition.key] ?? "").isEmpty)
            .accessibilityLabel(
                "Use inherited \(concreteDefaultLabel(for: definition)) for \(jobLabel)"
            )
            .help("Use inherited value: \(concreteDefaultLabel(for: definition))")
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var choicesIncludingCurrent: [ChoiceOption] {
        guard case .choice(let choices) = definition.kind,
              let current = envStore.values[definition.key],
              !current.isEmpty,
              !choices.contains(where: { $0.value == current }) else {
            if case .choice(let choices) = definition.kind { return choices }
            return []
        }
        return choices + [
            ChoiceOption(current, label: "From .env · \(hourChoiceLabel(current))")
        ]
    }

    private var jobLabel: String {
        definition.title.replacingOccurrences(of: " schedule (hours)", with: "")
    }
}

struct ImpactLine: View {
    let icon: String
    let label: String
    let value: String

    var body: some View {
        HStack(alignment: .top, spacing: 6) {
            Image(systemName: icon).foregroundStyle(.secondary).frame(width: 14)
            VStack(alignment: .leading, spacing: 1) {
                Text(label).font(.system(size: 10, weight: .semibold)).foregroundStyle(.secondary)
                Text(value).font(.system(size: 11)).lineLimit(2)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

struct ScheduleHint: View {
    let value: String
    let defaultValue: String

    var body: some View {
        let explicit = !value.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        let effectiveValue = explicit ? value : defaultValue
        let label = explicit
            ? humanSchedule(effectiveValue)
            : "\(humanSchedule(effectiveValue)) · inherited"
        Label(label, systemImage: "clock")
            .font(.system(size: 10, weight: .medium))
            .foregroundStyle(.secondary)
            .padding(.leading, 10)
            .accessibilityLabel("Schedule: \(label)")
    }
}

struct ImpactBadge: View {
    let impact: String

    var body: some View {
        let presentation: (String, Color) = {
            switch impact {
            case "read_only": return ("Read only", .blue)
            case "memory_write": return ("Memory write", .orange)
            case "external_write": return ("GitHub write", .purple)
            case "code_write": return ("Code & PR write", .red)
            default: return (impact, .secondary)
            }
        }()
        StatusBadge(text: presentation.0, color: presentation.1)
    }
}

struct StatusBadge: View {
    let text: String
    let color: Color

    var body: some View {
        Text(text)
            .font(.system(size: 10, weight: .semibold))
            .foregroundStyle(color)
            .padding(.horizontal, 7)
            .padding(.vertical, 3)
            .background(color.opacity(0.12), in: Capsule())
    }
}

struct SettingsWarningBox: View {
    let messages: [String]

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            ForEach(messages, id: \.self) { message in
                Label(message, systemImage: "exclamationmark.triangle.fill")
                    .font(.system(size: 11))
            }
        }
        .foregroundStyle(.orange)
        .padding(10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.orange.opacity(0.08), in: RoundedRectangle(cornerRadius: 6))
    }
}

struct EnvSettingSection: View {
    let title: String
    let definitions: [EnvSettingDefinition]
    @ObservedObject var envStore: EnvSettingsStore

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(title)
                .font(.system(size: 13, weight: .semibold, design: .rounded))
            VStack(spacing: 0) {
                ForEach(definitions) { definition in
                    EnvSettingRow(definition: definition, envStore: envStore)
                    if definition.id != definitions.last?.id {
                        Divider()
                    }
                }
            }
            .background(Color(nsColor: .controlBackgroundColor), in: RoundedRectangle(cornerRadius: 8))
        }
    }
}

struct EnvSettingRow: View {
    let definition: EnvSettingDefinition
    @ObservedObject var envStore: EnvSettingsStore
    var showKey = true

    var body: some View {
        HStack(alignment: .center, spacing: 12) {
            VStack(alignment: .leading, spacing: 3) {
                Text(definition.title)
                    .font(.system(size: 12, weight: .semibold))
                Text(definition.detail)
                    .font(.system(size: 11, weight: .regular))
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
                if showKey {
                    Text(definition.key)
                        .font(.system(size: 10, weight: .medium, design: .monospaced))
                        .foregroundStyle(.tertiary)
                        .lineLimit(1)
                }
            }
            Spacer(minLength: 12)
            control
                .frame(width: showKey ? 240 : 190)
            Button {
                envStore.setValue("", for: definition.key)
            } label: {
                Image(systemName: "xmark.circle.fill")
                    .foregroundStyle(.tertiary)
            }
            .buttonStyle(.plain)
            .disabled((envStore.values[definition.key] ?? "").isEmpty)
            .accessibilityLabel(
                "Use inherited \(concreteDefaultLabel(for: definition)) for \(definition.title)"
            )
            .help("Use inherited value: \(concreteDefaultLabel(for: definition))")
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 9)
    }

    @ViewBuilder
    private var control: some View {
        switch definition.kind {
        case .toggle:
            Picker("", selection: effectiveToggleSelection) {
                Text("On").tag("true")
                Text("Off").tag("false")
            }
            .labelsHidden()
            .pickerStyle(.segmented)
        case .choice(let choices):
            Picker("", selection: envStore.binding(for: definition.key)) {
                Text(inheritedDefaultLabel(for: definition)).tag("")
                ForEach(choicesIncludingCurrent(choices), id: \.value) { choice in
                    Text(choice.label).tag(choice.value)
                }
            }
            .labelsHidden()
            .pickerStyle(.menu)
        case .model(let choices):
            VStack(alignment: .trailing, spacing: 4) {
                Picker("", selection: envStore.binding(for: definition.key)) {
                    Text(inheritedDefaultLabel(for: definition)).tag("")
                    ForEach(choicesIncludingCurrent(choices), id: \.value) { choice in
                        Text(choice.label).tag(choice.value)
                    }
                }
                .labelsHidden()
                .pickerStyle(.menu)
                Text("Custom values: Advanced .env")
                    .font(.system(size: 9))
                    .foregroundStyle(.tertiary)
            }
        }
    }

    private var effectiveToggleSelection: Binding<String> {
        Binding(
            get: {
                let explicit = envStore.values[definition.key] ?? ""
                return normalizedToggleValue(explicit)
                    ?? normalizedToggleValue(definition.defaultValue)
                    ?? "false"
            },
            set: { envStore.setValue($0, for: definition.key) }
        )
    }

    private func choicesIncludingCurrent(_ choices: [ChoiceOption]) -> [ChoiceOption] {
        guard let current = envStore.values[definition.key],
              !current.isEmpty,
              !choices.contains(where: { $0.value == current }) else {
            return choices
        }
        let label = definition.key.hasSuffix("_S")
            ? "From .env · \(hourChoiceLabel(current))"
            : "From .env · \(current)"
        return choices + [ChoiceOption(current, label: label)]
    }
}

private func normalizedToggleValue(_ raw: String) -> String? {
    switch raw.trimmingCharacters(in: .whitespacesAndNewlines).lowercased() {
    case "1", "true", "yes", "on": return "true"
    case "0", "false", "no", "off": return "false"
    default: return nil
    }
}

private func concreteDefaultLabel(for definition: EnvSettingDefinition) -> String {
    let raw = definition.defaultValue.trimmingCharacters(in: .whitespacesAndNewlines)
    switch definition.kind {
    case .toggle:
        if raw.lowercased() == "true" { return "On" }
        if raw.lowercased() == "false" { return "Off" }
        return raw.isEmpty ? "Off" : raw
    case .choice(let choices):
        if let option = choices.first(where: { $0.value == raw }) {
            return option.label
        }
        if definition.key.hasSuffix("_S") {
            return hourChoiceLabel(raw)
        }
        return raw.isEmpty ? "Inherited" : raw
    case .model:
        return raw.isEmpty ? "CLI managed" : raw
    }
}

private func inheritedDefaultLabel(for definition: EnvSettingDefinition) -> String {
    "\(concreteDefaultLabel(for: definition)) · inherited"
}

private func formattedHours(_ hours: Double) -> String {
    var text = String(format: "%.3f", hours)
    while text.contains(".") && text.last == "0" { text.removeLast() }
    if text.last == "." { text.removeLast() }
    return text
}

private func hourChoiceLabel(_ rawSeconds: String) -> String {
    guard let seconds = Double(rawSeconds), seconds.isFinite, seconds >= 0 else {
        return "Custom .env value"
    }
    guard seconds > 0 else { return "Off" }
    let hours = seconds / 3600
    let hourText = "\(formattedHours(hours)) h"
    if hours >= 24, hours.truncatingRemainder(dividingBy: 24) == 0 {
        let days = Int(hours / 24)
        return "\(hourText) · \(days) \(days == 1 ? "day" : "days")"
    }
    return hourText
}

private func humanInterval(_ seconds: Double) -> String {
    if seconds <= 0 { return "just now" }
    if seconds < 3600 { return "<1 h" }
    return hourChoiceLabel(String(seconds))
}

private func humanSchedule(_ rawValue: String) -> String {
    let raw = rawValue.trimmingCharacters(in: .whitespacesAndNewlines)
    guard !raw.isEmpty else { return "Uses default schedule" }
    guard let seconds = Double(raw), seconds.isFinite else {
        return "Custom schedule is managed in Advanced .env"
    }
    guard seconds > 0 else { return "Off" }
    return "Every \(hourChoiceLabel(raw))"
}

struct AgentStatusMenu: View {
    @EnvironmentObject var store: AgentStatusStore

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 10) {
                Image(systemName: "memorychip")
                    .font(.system(size: 16, weight: .semibold))
                    .foregroundStyle(.blue)
                VStack(alignment: .leading, spacing: 1) {
                    Text("ThreadKeeper")
                        .font(.system(size: 14, weight: .semibold, design: .rounded))
                    Text("Autonomous learning loops")
                        .font(.system(size: 11, weight: .medium))
                        .foregroundStyle(.secondary)
                }
                Spacer()
                Text(store.isThreadKeeperDisabled ? "Off" : "\(store.snapshot.runningLoopCount)/\(store.snapshot.enabledLoopCount)")
                    .font(.system(size: 12, weight: .semibold, design: .rounded))
                    .foregroundStyle(store.isThreadKeeperDisabled ? .secondary : .primary)
                    .padding(.horizontal, 8)
                    .padding(.vertical, 3)
                    .background(Color(nsColor: .controlBackgroundColor), in: Capsule())
                Button {
                    store.openEnvSettings()
                } label: {
                    Image(systemName: "gearshape")
                        .font(.system(size: 13, weight: .semibold))
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Open ThreadKeeper Settings")
                .help("Settings")
            }
            .padding(.horizontal, 12)
            .padding(.top, 12)
            .padding(.bottom, 10)

            Divider()

            ThreadKeeperToggleBar()
                .padding(.horizontal, 12)
                .padding(.vertical, 10)

            Divider()

            ScrollView {
                VStack(spacing: 8) {
                    if let error = store.lastError {
                        ErrorState(message: error)
                    } else if store.snapshot.loops.isEmpty {
                        EmptyState()
                    } else {
                        ForEach(store.snapshot.loops) { loop in
                            LoopRow(loop: loop)
                        }
                    }
                }
                .padding(12)
            }
            .frame(maxHeight: 520)

            Divider()
            HStack(spacing: 12) {
                MetricLabel(systemImage: "play.circle", text: "\(store.snapshot.runningLoopCount) running")
                MetricLabel(systemImage: "bolt.circle", text: "\(store.snapshot.readyLoopCount) ready")
                MetricLabel(systemImage: "memorychip", text: "\(store.snapshot.totalRssMb) MB child RSS")
                Spacer()
                Button {
                    store.cleanMemory()
                } label: {
                    Image(systemName: "trash")
                        .font(.system(size: 14, weight: .semibold))
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Clean ThreadKeeper memory")
                .help("Clean memory")
                Button {
                    NSApplication.shared.terminate(nil)
                } label: {
                    Image(systemName: "xmark.circle")
                        .font(.system(size: 14, weight: .semibold))
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Quit ThreadKeeper")
                .help("Quit")
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 10)
        }
        .frame(width: panelWidth)
        .background(.regularMaterial)
    }
}

private let gearSpinInterval: TimeInterval = 0.075
private let gearFrameStepDegrees = 17.0
private let statusIconSize = NSSize(width: 22, height: 18)
private let largeGearDiameter: CGFloat = 12.0
private let smallGearDiameter: CGFloat = 9.0
private let gearMeshPhaseDegrees: CGFloat = 15.0

@MainActor
final class StatusItemController: NSObject {
    private let store = AgentStatusStore()
    private let statusItem: NSStatusItem
    private let popover: NSPopover
    private let idleImage: NSImage
    private let errorImage: NSImage
    private let gearFrames: [NSImage]
    private var animationTimer: Timer?
    private var frameIndex = 0

    override init() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        popover = NSPopover()
        idleImage = Self.makeTemplateSymbolImage("memorychip")
        errorImage = Self.makeTemplateSymbolImage("exclamationmark.triangle")
        gearFrames = Self.makeGearFrames()
        super.init()

        configureStatusButton()
        configurePopover()
        store.start()
        updateStatusButton()
        animationTimer = Timer(timeInterval: gearSpinInterval, repeats: true) { [weak self] _ in
            Task { @MainActor in
                self?.updateStatusButton()
            }
        }
        if let animationTimer {
            RunLoop.main.add(animationTimer, forMode: .common)
        }
    }

    func invalidate() {
        animationTimer?.invalidate()
        animationTimer = nil
        NSStatusBar.system.removeStatusItem(statusItem)
    }

    func openSettings() {
        popover.performClose(nil)
        store.openEnvSettings()
    }

    private func configureStatusButton() {
        guard let button = statusItem.button else {
            return
        }
        button.target = self
        button.action = #selector(togglePopover(_:))
        button.imagePosition = .imageOnly
        button.font = .systemFont(ofSize: 14, weight: .medium)
        button.toolTip = "ThreadKeeper status"
        button.setAccessibilityLabel("ThreadKeeper status")
    }

    private func configurePopover() {
        popover.behavior = .transient
        popover.contentSize = NSSize(width: panelWidth, height: 560)
        popover.contentViewController = NSHostingController(
            rootView: AgentStatusMenu()
                .environmentObject(store)
        )
    }

    @objc private func togglePopover(_ sender: Any?) {
        guard let button = statusItem.button else {
            return
        }
        if popover.isShown {
            popover.performClose(sender)
            return
        }
        popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
        store.refresh()
        updateStatusButton()
    }

    private func updateStatusButton() {
        guard let button = statusItem.button else {
            return
        }

        if store.lastError != nil {
            frameIndex = 0
            button.image = errorImage
            button.title = ""
            button.toolTip = "ThreadKeeper status error"
            button.setAccessibilityLabel("ThreadKeeper status error")
            return
        }

        if store.isThreadKeeperDisabled {
            frameIndex = 0
            button.image = idleImage
            button.title = ""
            button.toolTip = "ThreadKeeper off: background daemons paused"
            button.setAccessibilityLabel("ThreadKeeper off: background daemons paused")
            return
        }

        let summary = statusSummary
        if store.snapshot.runningLoopCount > 0 {
            button.image = gearFrames[frameIndex % gearFrames.count]
            button.title = ""
            button.toolTip = "ThreadKeeper loops running: \(summary)"
            button.setAccessibilityLabel("ThreadKeeper loops running: \(summary)")
            frameIndex = (frameIndex + 1) % gearFrames.count
        } else {
            frameIndex = 0
            button.image = idleImage
            button.title = ""
            button.toolTip = "ThreadKeeper idle: \(summary)"
            button.setAccessibilityLabel("ThreadKeeper idle: \(summary)")
        }
    }

    private var statusSummary: String {
        if store.isThreadKeeperDisabled {
            return "background daemons off"
        }
        if store.snapshot.runningLoopCount == 0 {
            return "\(store.snapshot.enabledLoopCount) loops enabled"
        }
        return "\(store.snapshot.runningLoopCount)/\(store.snapshot.enabledLoopCount) loops running"
    }

    private static func makeGearFrames() -> [NSImage] {
        // Avoid 45-degree steps: gearshape.fill is rotationally symmetric, so
        // one-tooth increments can render as the same frame.
        stride(from: 0.0, to: 360.0, by: gearFrameStepDegrees).map { angle in
            makeGearFrame(angle: CGFloat(angle))
        }
    }

    private static func makeGearFrame(angle: CGFloat) -> NSImage {
        let image = NSImage(size: statusIconSize)
        image.lockFocus()
        defer {
            image.unlockFocus()
            image.isTemplate = true
        }

        let smallAngle = (
            -angle * largeGearDiameter / smallGearDiameter
        ) + gearMeshPhaseDegrees

        drawGearSymbol(
            center: NSPoint(x: 7.0, y: 11.0),
            diameter: largeGearDiameter,
            angle: angle
        )
        drawGearSymbol(
            center: NSPoint(x: 15.0, y: 6.0),
            diameter: smallGearDiameter,
            angle: smallAngle
        )
        return image
    }

    private static func drawGearSymbol(
        center: NSPoint,
        diameter: CGFloat,
        angle: CGFloat
    ) {
        guard let gear = NSImage(systemSymbolName: "gearshape.fill", accessibilityDescription: nil) else {
            return
        }

        let rect = NSRect(
            x: center.x - diameter / 2.0,
            y: center.y - diameter / 2.0,
            width: diameter,
            height: diameter
        )

        NSGraphicsContext.saveGraphicsState()
        defer {
            NSGraphicsContext.restoreGraphicsState()
        }
        NSGraphicsContext.current?.imageInterpolation = .high
        let transform = NSAffineTransform()
        transform.translateX(by: center.x, yBy: center.y)
        transform.rotate(byDegrees: angle)
        transform.translateX(by: -center.x, yBy: -center.y)
        transform.concat()
        gear.draw(in: rect, from: .zero, operation: .sourceOver, fraction: 1.0)
    }

    private static func makeTemplateSymbolImage(_ symbolName: String) -> NSImage {
        if let symbol = NSImage(systemSymbolName: symbolName, accessibilityDescription: nil) {
            symbol.isTemplate = true
            return symbol
        }
        let fallback = NSImage(size: statusIconSize)
        fallback.isTemplate = true
        return fallback
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    private var statusItemController: StatusItemController?

    func openSettings() {
        Task { @MainActor in
            statusItemController?.openSettings()
        }
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        Task { @MainActor in
            statusItemController = StatusItemController()
        }
    }

    func applicationWillTerminate(_ notification: Notification) {
        Task { @MainActor in
            statusItemController?.invalidate()
            statusItemController = nil
        }
    }
}

#if !THREADKEEPER_SETTINGS_TEST
@main
struct ThreadKeeperAgentStatusApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate

    var body: some Scene {
        Settings {
            EmptyView()
        }
        .commands {
            CommandGroup(replacing: .appSettings) {
                Button("Settings…") {
                    appDelegate.openSettings()
                }
                .keyboardShortcut(",", modifiers: .command)
            }
        }
    }
}
#endif
