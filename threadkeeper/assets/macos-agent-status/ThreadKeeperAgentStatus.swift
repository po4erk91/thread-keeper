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

    enum CodingKeys: String, CodingKey {
        case id
        case taskId = "task_id"
        case loopName = "loop_name"
        case title
        case summary
        case age
    }
}

private let panelWidth: CGFloat = 380
private let settingsWindowWidth: CGFloat = 760
private let settingsWindowHeight: CGFloat = 720

enum EnvSettingKind {
    case toggle
    case number
    case text
    case choice([String])
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

private let cliChoices = ["claude", "codex", "antigravity", "agy", "gemini", "copilot"]

private let envSettingDefinitions: [EnvSettingDefinition] = [
    EnvSettingDefinition(
        group: "Core",
        key: "THREADKEEPER_DISABLE_BG_DAEMONS",
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
        kind: .number
    ),
    EnvSettingDefinition(
        group: "Core",
        key: "THREADKEEPER_AUTO_UPDATE_INTERVAL_S",
        title: "Auto-update interval",
        detail: "Seconds between MCP self-update checks; 0 disables.",
        defaultValue: "86400",
        kind: .number
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
        group: "Learning Loops",
        key: "THREADKEEPER_SHADOW_REVIEW_INTERVAL_S",
        title: "Shadow review interval",
        detail: "Reviews closed work for memory or skill candidates.",
        defaultValue: "0",
        kind: .number
    ),
    EnvSettingDefinition(
        group: "Learning Loops",
        key: "THREADKEEPER_CURATOR_INTERVAL_S",
        title: "Curator interval",
        detail: "Audits and consolidates materialized lessons.",
        defaultValue: "0",
        kind: .number
    ),
    EnvSettingDefinition(
        group: "Learning Loops",
        key: "THREADKEEPER_EXTRACT_INTERVAL_S",
        title: "Extract interval",
        detail: "Mines recent dialog for structured memory candidates.",
        defaultValue: "0",
        kind: .number
    ),
    EnvSettingDefinition(
        group: "Learning Loops",
        key: "THREADKEEPER_CANDIDATE_REVIEW_INTERVAL_S",
        title: "Candidate review interval",
        detail: "Promotes or rejects extracted candidates.",
        defaultValue: "0",
        kind: .number
    ),
    EnvSettingDefinition(
        group: "Learning Loops",
        key: "THREADKEEPER_EVOLVE_REVIEW_INTERVAL_S",
        title: "Evolve review interval",
        detail: "Reviews proposed code or docs evolution work.",
        defaultValue: "0",
        kind: .number
    ),
    EnvSettingDefinition(
        group: "Learning Loops",
        key: "THREADKEEPER_EVOLVE_APPLY_INTERVAL_S",
        title: "Evolve apply interval",
        detail: "Applies promoted evolve work; keep deliberate.",
        defaultValue: "0",
        kind: .number
    ),
    EnvSettingDefinition(
        group: "Learning Loops",
        key: "THREADKEEPER_THREAD_JANITOR_INTERVAL_S",
        title: "Thread janitor interval",
        detail: "Closes idle working-memory threads.",
        defaultValue: "0",
        kind: .number
    ),
    EnvSettingDefinition(
        group: "Learning Loops",
        key: "THREADKEEPER_PROBE_INTERVAL_S",
        title: "Probe interval",
        detail: "Runs reliability probes; 1800 is a typical active value.",
        defaultValue: "0",
        kind: .number
    ),
    EnvSettingDefinition(
        group: "Learning Loops",
        key: "THREADKEEPER_DIALECTIC_MINE_INTERVAL_S",
        title: "Dialectic mine interval",
        detail: "Finds candidate claims from dialog.",
        defaultValue: "0",
        kind: .number
    ),
    EnvSettingDefinition(
        group: "Learning Loops",
        key: "THREADKEEPER_DIALECTIC_VALIDATE_INTERVAL_S",
        title: "Dialectic validate interval",
        detail: "Validates queued dialectic claims.",
        defaultValue: "0",
        kind: .number
    ),

    EnvSettingDefinition(
        group: "Memory And Budgets",
        key: "THREADKEEPER_MEMORY_NUDGE_INTERVAL",
        title: "Memory nudge interval",
        detail: "Number of events before the memory-save nudge appears.",
        defaultValue: "10",
        kind: .number
    ),
    EnvSettingDefinition(
        group: "Memory And Budgets",
        key: "THREADKEEPER_SKILL_NUDGE_INTERVAL",
        title: "Skill nudge interval",
        detail: "Number of events before skill-materialization checks.",
        defaultValue: "10",
        kind: .number
    ),
    EnvSettingDefinition(
        group: "Memory And Budgets",
        key: "THREADKEEPER_SPAWN_BUDGET_MB",
        title: "Spawn budget",
        detail: "Aggregate child-agent RSS budget in MB; 0 disables.",
        defaultValue: "3072",
        kind: .number
    ),
    EnvSettingDefinition(
        group: "Memory And Budgets",
        key: "THREADKEEPER_MEMORY_GUARD_WARN_MB",
        title: "Memory guard warn",
        detail: "Warn threshold per server process, in MB.",
        defaultValue: "1536",
        kind: .number
    ),
    EnvSettingDefinition(
        group: "Memory And Budgets",
        key: "THREADKEEPER_MEMORY_GUARD_KILL_MB",
        title: "Memory guard kill",
        detail: "Kill threshold per server process, in MB.",
        defaultValue: "3072",
        kind: .number
    ),
    EnvSettingDefinition(
        group: "Memory And Budgets",
        key: "THREADKEEPER_MEMORY_GUARD_TARGET_SERVERS",
        title: "Target server count",
        detail: "Number of MCP server processes to keep after reclaim.",
        defaultValue: "1",
        kind: .number
    ),

    EnvSettingDefinition(
        group: "Spawn Routing",
        key: "THREADKEEPER_SPAWN__DEFAULT",
        title: "Default CLI",
        detail: "Fallback agent CLI when a role has no explicit route.",
        defaultValue: "",
        kind: .choice(cliChoices)
    ),
    EnvSettingDefinition(
        group: "Spawn Routing",
        key: "THREADKEEPER_SPAWN__LOOP__EVOLVE_APPLIER",
        title: "Evolve applier CLI",
        detail: "CLI used by the role that writes code and opens PRs.",
        defaultValue: "claude",
        kind: .choice(cliChoices)
    ),
    EnvSettingDefinition(
        group: "Spawn Routing",
        key: "THREADKEEPER_SPAWN__MODEL__CLAUDE",
        title: "Claude model",
        detail: "Model pin for Claude-backed spawned agents.",
        defaultValue: "sonnet",
        kind: .text
    ),
    EnvSettingDefinition(
        group: "Spawn Routing",
        key: "THREADKEEPER_SPAWN__MODEL__AGY",
        title: "Antigravity model",
        detail: "Model pin for Antigravity/agy spawned agents.",
        defaultValue: "gemini-3.1-pro",
        kind: .text
    ),
    EnvSettingDefinition(
        group: "Spawn Routing",
        key: "THREADKEEPER_SPAWN__MODEL__EVOLVE_APPLIER",
        title: "Evolve applier model",
        detail: "Model pin for the code-writing evolve role.",
        defaultValue: "opus",
        kind: .text
    ),
    EnvSettingDefinition(
        group: "Spawn Routing",
        key: "THREADKEEPER_SPAWN__MODEL__DIALECTIC_VALIDATOR",
        title: "Dialectic validator model",
        detail: "Model pin for claim-validation work.",
        defaultValue: "opus",
        kind: .text
    ),
]

private var envSettingGroups: [String] {
    var groups: [String] = []
    for definition in envSettingDefinitions where !groups.contains(definition.group) {
        groups.append(definition.group)
    }
    return groups
}

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

    private weak var agentStore: AgentStatusStore?
    private let presetDefaultsKey = "threadkeeperEnvPresetSlotsV1"
    let envFileURL: URL

    var canSave: Bool {
        validationMessages.isEmpty && !isSaving
    }

    var envPath: String {
        envFileURL.path
    }

    init(agentStore: AgentStatusStore?) {
        self.agentStore = agentStore
        self.envFileURL = Self.resolveEnvFileURL()
        loadPresetSlots()
        loadEnv()
    }

    func binding(for key: String) -> Binding<String> {
        Binding(
            get: { self.values[key] ?? "" },
            set: { self.setValue($0, for: key) }
        )
    }

    func setValue(_ value: String, for key: String) {
        let cleaned = value.replacingOccurrences(of: "\n", with: " ")
        if cleaned.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            values.removeValue(forKey: key)
        } else {
            values[key] = cleaned.trimmingCharacters(in: .whitespacesAndNewlines)
        }
        validate()
    }

    func loadEnv() {
        do {
            if FileManager.default.fileExists(atPath: envFileURL.path) {
                rawEnvText = try String(contentsOf: envFileURL, encoding: .utf8)
                values = Self.extractKnownValues(from: rawEnvText)
                statusMessage = "Loaded \(envFileURL.path)"
            } else {
                rawEnvText = ""
                values = [:]
                statusMessage = "A new .env will be created at \(envFileURL.path)"
            }
        } catch {
            statusMessage = "Could not read .env: \(error.localizedDescription)"
        }
        validate()
    }

    func importRawIntoForm() {
        values = Self.extractKnownValues(from: rawEnvText)
        validate()
        statusMessage = "Imported raw .env values into the form."
    }

    func syncRawEditsIntoForm() {
        values = Self.extractKnownValues(from: rawEnvText)
        validate()
    }

    func syncFormToRaw() {
        rawEnvText = Self.mergeEnvText(raw: rawEnvText, values: values)
        validate()
        statusMessage = "Updated the raw .env preview from the form."
    }

    func save(restart: Bool) {
        validate()
        guard canSave else {
            statusMessage = "Fix the highlighted settings before saving."
            return
        }

        isSaving = true
        defer {
            isSaving = false
        }

        do {
            let merged = Self.mergeEnvText(raw: rawEnvText, values: values)
            try FileManager.default.createDirectory(
                at: envFileURL.deletingLastPathComponent(),
                withIntermediateDirectories: true
            )
            try merged.write(to: envFileURL, atomically: true, encoding: .utf8)
            rawEnvText = merged
            statusMessage = "Saved \(envFileURL.path)"

            if restart {
                try requestThreadKeeperRestart()
                statusMessage = "Saved and requested ThreadKeeper restart."
            }
            agentStore?.refresh()
        } catch {
            statusMessage = "Save failed: \(error.localizedDescription)"
        }
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
        let merged = Self.mergeEnvText(raw: rawEnvText, values: values)
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
            ? Self.extractKnownValues(from: preset.rawEnvText)
            : preset.values
        validate()
        statusMessage = "Loaded \(preset.name)."
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
        for definition in envSettingDefinitions {
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
            case .number:
                if Double(value) == nil {
                    messages.append("\(definition.title) must be a number.")
                } else if let number = Double(value), number < 0 {
                    messages.append("\(definition.title) cannot be negative.")
                }
            case .choice(let choices):
                if !choices.contains(value) {
                    messages.append("\(definition.title) must be one of: \(choices.joined(separator: ", ")).")
                }
            case .text:
                break
            }
        }

        appendThresholdWarning(
            to: &messages,
            warnKey: "THREADKEEPER_MEMORY_GUARD_WARN_MB",
            killKey: "THREADKEEPER_MEMORY_GUARD_KILL_MB",
            label: "Memory guard kill"
        )
        validationMessages = messages
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

    private static func resolveEnvFileURL() -> URL {
        let env = ProcessInfo.processInfo.environment
        let raw = env["THREADKEEPER_ENV_FILE"]?.trimmingCharacters(in: .whitespacesAndNewlines)
        let path = raw?.isEmpty == false
            ? raw!
            : "~/.threadkeeper/.env"
        return URL(fileURLWithPath: (path as NSString).expandingTildeInPath)
    }

    private static func extractKnownValues(from text: String) -> [String: String] {
        let definitionsByKey = Dictionary(
            uniqueKeysWithValues: envSettingDefinitions.map { ($0.key, $0) }
        )
        var parsedValues: [String: String] = [:]
        for line in text.components(separatedBy: .newlines) {
            guard let parsed = parseEnvLine(line),
                  !parsed.isCommented,
                  let definition = definitionsByKey[parsed.key] else {
                continue
            }
            parsedValues[parsed.key] = normalizedValue(parsed.value, for: definition)
        }
        return parsedValues
    }

    private static func mergeEnvText(raw: String, values: [String: String]) -> String {
        let knownKeys = Set(envSettingDefinitions.map(\.key))
        var seen: Set<String> = []
        var output: [String] = []

        for line in raw.components(separatedBy: .newlines) {
            guard let parsed = parseEnvLine(line),
                  knownKeys.contains(parsed.key) else {
                output.append(line)
                continue
            }

            guard !seen.contains(parsed.key) else {
                output.append(parsed.isCommented ? line : "# \(line)")
                continue
            }
            seen.insert(parsed.key)

            let value = values[parsed.key]?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
            if value.isEmpty {
                output.append(parsed.isCommented ? line : "# \(parsed.key)=")
            } else {
                output.append("\(parsed.key)=\(formatEnvValue(value))")
            }
        }

        let missing = envSettingDefinitions.filter {
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

    private static func isBoolValue(_ value: String) -> Bool {
        ["true", "false", "1", "0", "yes", "no", "on", "off"]
            .contains(value.lowercased())
    }

    private static func normalizedValue(_ value: String, for definition: EnvSettingDefinition) -> String {
        guard case .toggle = definition.kind else {
            return value
        }
        switch value.lowercased() {
        case "true", "1", "yes", "on":
            return "true"
        case "false", "0", "no", "off":
            return "false"
        default:
            return value
        }
    }
}

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
        agents: []
    )
    @Published var lastError: String?

    private var timer: Timer?
    private var envSettingsWindowController: EnvSettingsWindowController?
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
        refresh()
        timer = Timer.scheduledTimer(withTimeInterval: 5.0, repeats: true) { [weak self] _ in
            Task { @MainActor in
                self?.refresh()
            }
        }
    }

    func refresh() {
        do {
            let newSnapshot = try autoreleasepool {
                let data = try runStatusCommand(arguments: ["--json"])
                return try JSONDecoder().decode(AgentStatusSnapshot.self, from: data)
            }
            handleUsefulResults(newSnapshot.recentResults)
            snapshot = newSnapshot
            lastError = nil
            checkAppMemoryPressure(reason: "poll")
        } catch {
            lastError = error.localizedDescription
        }
    }

    func cleanMemory() {
        do {
            _ = try autoreleasepool {
                try runStatusCommand(arguments: ["--cleanup-memory"])
            }
            refresh()
            checkAppMemoryPressure(reason: "manual-cleanup")
        } catch {
            lastError = error.localizedDescription
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
            postNotification(for: result)
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

    func runStatusCommand(arguments: [String]) throws -> Data {
        let process = Process()
        let pipe = Pipe()
        let errPipe = Pipe()
        let command = statusCommand()

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
        process.waitUntilExit()

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

    private func statusCommand() -> (executable: String, arguments: [String]) {
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
        if window?.isVisible != true {
            envStore.loadEnv()
        }
        showWindow(nil)
        window?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }
}

struct EnvSettingsView: View {
    @ObservedObject var envStore: EnvSettingsStore

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            TabView {
                guidedTab
                    .tabItem {
                        Label("Guided", systemImage: "slider.horizontal.3")
                    }
                rawTab
                    .tabItem {
                        Label("Raw .env", systemImage: "doc.text")
                    }
            }
            .padding(.horizontal, 16)
            .padding(.top, 12)
            Divider()
            footer
        }
        .frame(minWidth: 680, minHeight: 620)
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
            Button {
                envStore.loadEnv()
            } label: {
                Image(systemName: "arrow.clockwise")
                    .font(.system(size: 13, weight: .semibold))
            }
            .buttonStyle(.plain)
            .help("Reload .env")
        }
        .padding(16)
    }

    private var guidedTab: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                presetSection
                ForEach(envSettingGroups, id: \.self) { group in
                    EnvSettingSection(
                        title: group,
                        definitions: envSettingDefinitions.filter { $0.group == group },
                        envStore: envStore
                    )
                }
            }
            .padding(.bottom, 14)
        }
    }

    private var presetSection: some View {
        VStack(alignment: .leading, spacing: 10) {
            Label("Presets", systemImage: "square.grid.3x1.fill")
                .font(.system(size: 13, weight: .semibold, design: .rounded))
            HStack(alignment: .top, spacing: 10) {
                ForEach(envStore.presetSlots) { preset in
                    EnvPresetCard(preset: preset, envStore: envStore)
                }
            }
        }
    }

    private var rawTab: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 8) {
                Label("Raw .env", systemImage: "doc.text")
                    .font(.system(size: 13, weight: .semibold, design: .rounded))
                Spacer()
                Button {
                    envStore.importRawIntoForm()
                } label: {
                    Label("Import", systemImage: "square.and.arrow.down")
                }
                Button {
                    envStore.syncFormToRaw()
                } label: {
                    Label("Preview", systemImage: "text.badge.checkmark")
                }
            }
            TextEditor(text: $envStore.rawEnvText)
                .font(.system(size: 12, weight: .regular, design: .monospaced))
                .frame(minHeight: 470)
                .overlay(
                    RoundedRectangle(cornerRadius: 8)
                        .stroke(Color(nsColor: .separatorColor).opacity(0.5))
                )
                .onChange(of: envStore.rawEnvText) { _ in
                    envStore.syncRawEditsIntoForm()
                }
        }
        .padding(.bottom, 14)
    }

    private var footer: some View {
        HStack(spacing: 10) {
            Image(systemName: envStore.validationMessages.isEmpty ? "checkmark.circle" : "exclamationmark.triangle.fill")
                .foregroundStyle(envStore.validationMessages.isEmpty ? .green : .orange)
            Text(footerMessage)
                .font(.system(size: 11, weight: .medium))
                .foregroundStyle(envStore.validationMessages.isEmpty ? .secondary : .primary)
                .lineLimit(2)
            Spacer()
            Button {
                envStore.save(restart: false)
            } label: {
                Label("Save", systemImage: "square.and.arrow.down")
            }
            .disabled(!envStore.canSave)
            Button {
                envStore.save(restart: true)
            } label: {
                Label("Save & Restart", systemImage: "arrow.clockwise.circle")
            }
            .keyboardShortcut(.defaultAction)
            .disabled(!envStore.canSave)
        }
        .padding(16)
    }

    private var footerMessage: String {
        if let first = envStore.validationMessages.first {
            let remaining = envStore.validationMessages.count - 1
            return remaining > 0 ? "\(first) +\(remaining) more" : first
        }
        return envStore.statusMessage
    }
}

struct EnvPresetCard: View {
    let preset: EnvPresetSlot
    @ObservedObject var envStore: EnvSettingsStore

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            TextField(
                "Preset name",
                text: Binding(
                    get: { preset.name },
                    set: { envStore.renamePreset(slot: preset.slot, name: $0) }
                )
            )
            .font(.system(size: 12, weight: .medium))
            .textFieldStyle(.roundedBorder)
            Text(preset.values.isEmpty ? "Empty" : "\(preset.values.count) settings")
                .font(.system(size: 11, weight: .medium))
                .foregroundStyle(.secondary)
            HStack(spacing: 6) {
                Button {
                    envStore.loadPreset(slot: preset.slot)
                } label: {
                    Image(systemName: "tray.and.arrow.down")
                }
                .buttonStyle(.plain)
                .disabled(preset.values.isEmpty && preset.rawEnvText.isEmpty)
                .help("Load preset")
                Button {
                    envStore.savePreset(slot: preset.slot)
                } label: {
                    Image(systemName: "tray.and.arrow.up")
                }
                .buttonStyle(.plain)
                .help("Save current settings to preset")
                Button {
                    envStore.clearPreset(slot: preset.slot)
                } label: {
                    Image(systemName: "xmark.circle")
                }
                .buttonStyle(.plain)
                .disabled(preset.values.isEmpty && preset.rawEnvText.isEmpty)
                .help("Clear preset")
            }
        }
        .padding(10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color(nsColor: .controlBackgroundColor), in: RoundedRectangle(cornerRadius: 8))
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

    var body: some View {
        HStack(alignment: .center, spacing: 12) {
            VStack(alignment: .leading, spacing: 3) {
                Text(definition.title)
                    .font(.system(size: 12, weight: .semibold))
                Text(definition.detail)
                    .font(.system(size: 11, weight: .regular))
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
                Text(definition.key)
                    .font(.system(size: 10, weight: .medium, design: .monospaced))
                    .foregroundStyle(.tertiary)
                    .lineLimit(1)
            }
            Spacer(minLength: 12)
            control
                .frame(width: 180)
            Button {
                envStore.setValue("", for: definition.key)
            } label: {
                Image(systemName: "xmark.circle.fill")
                    .foregroundStyle(.tertiary)
            }
            .buttonStyle(.plain)
            .disabled((envStore.values[definition.key] ?? "").isEmpty)
            .help("Use default")
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 9)
    }

    @ViewBuilder
    private var control: some View {
        switch definition.kind {
        case .toggle:
            Picker("", selection: envStore.binding(for: definition.key)) {
                Text("Default").tag("")
                Text("On").tag("true")
                Text("Off").tag("false")
            }
            .labelsHidden()
            .pickerStyle(.segmented)
        case .number:
            TextField(definition.defaultValue, text: envStore.binding(for: definition.key))
                .textFieldStyle(.roundedBorder)
                .multilineTextAlignment(.trailing)
        case .text:
            TextField(definition.defaultValue, text: envStore.binding(for: definition.key))
                .textFieldStyle(.roundedBorder)
                .multilineTextAlignment(.trailing)
        case .choice(let choices):
            Picker("", selection: envStore.binding(for: definition.key)) {
                Text("Default").tag("")
                ForEach(choices, id: \.self) { choice in
                    Text(choice).tag(choice)
                }
            }
            .labelsHidden()
            .pickerStyle(.menu)
        }
    }
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
                Text("\(store.snapshot.runningLoopCount)/\(store.snapshot.enabledLoopCount)")
                    .font(.system(size: 12, weight: .semibold, design: .rounded))
                    .foregroundStyle(.primary)
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
                .help("Settings")
            }
            .padding(.horizontal, 12)
            .padding(.top, 12)
            .padding(.bottom, 10)

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
                .help("Clean memory")
                Button {
                    NSApplication.shared.terminate(nil)
                } label: {
                    Image(systemName: "xmark.circle")
                        .font(.system(size: 14, weight: .semibold))
                }
                .buttonStyle(.plain)
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

    private func configureStatusButton() {
        guard let button = statusItem.button else {
            return
        }
        button.target = self
        button.action = #selector(togglePopover(_:))
        button.imagePosition = .imageOnly
        button.font = .systemFont(ofSize: 14, weight: .medium)
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
        store.refresh()
        updateStatusButton()
        popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
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

@main
struct ThreadKeeperAgentStatusApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate

    var body: some Scene {
        Settings {
            EmptyView()
        }
    }
}
