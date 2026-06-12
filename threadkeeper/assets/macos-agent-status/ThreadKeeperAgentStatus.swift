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
    private var didPrimeResults = false
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
            let data = try runStatusCommand()
            let newSnapshot = try JSONDecoder().decode(AgentStatusSnapshot.self, from: data)
            handleUsefulResults(newSnapshot.recentResults)
            snapshot = newSnapshot
            lastError = nil
        } catch {
            lastError = error.localizedDescription
        }
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

    private func runStatusCommand() throws -> Data {
        let process = Process()
        let pipe = Pipe()
        let errPipe = Pipe()
        let command = statusCommand()

        process.executableURL = URL(fileURLWithPath: command.executable)
        process.arguments = command.arguments + ["--json"]
        process.standardOutput = pipe
        process.standardError = errPipe

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
                    store.refresh()
                } label: {
                    Image(systemName: "arrow.clockwise")
                        .font(.system(size: 13, weight: .semibold))
                }
                .buttonStyle(.plain)
                .help("Refresh")
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

struct AgentStatusLabel: View {
    @EnvironmentObject var store: AgentStatusStore

    var body: some View {
        Group {
            if store.lastError != nil {
                Label("TK !", systemImage: "exclamationmark.triangle")
            } else if store.snapshot.runningLoopCount == 0 {
                Label("TK \(store.snapshot.enabledLoopCount)", systemImage: "gearshape.2")
            } else {
                Label(
                    "TK \(store.snapshot.runningLoopCount)/\(store.snapshot.enabledLoopCount)",
                    systemImage: "memorychip"
                )
            }
        }
        .onAppear {
            store.start()
        }
    }
}

@main
struct ThreadKeeperAgentStatusApp: App {
    @StateObject private var store = AgentStatusStore()

    var body: some Scene {
        MenuBarExtra {
            AgentStatusMenu()
                .environmentObject(store)
                .onAppear { store.start() }
        } label: {
            AgentStatusLabel()
                .environmentObject(store)
        }
        .menuBarExtraStyle(.window)
    }
}
