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

    private func runStatusCommand(arguments: [String]) throws -> Data {
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
