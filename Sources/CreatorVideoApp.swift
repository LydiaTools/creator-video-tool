import AppKit
import SwiftUI
import UniformTypeIdentifiers
import AVKit
import ImageIO

private enum ToolPage: String, CaseIterable, Identifiable {
    case video = "视频出海"
    case research = "找对标"
    case assets = "素材库"
    var id: String { rawValue }
    var symbol: String {
        switch self {
        case .video: "film.stack"
        case .research: "magnifyingglass"
        case .assets: "square.stack.3d.up"
        }
    }
}

private final class CommandControl: @unchecked Sendable {
    private let lock = NSLock()
    private var process: Process?
    private var cancelled = false

    func launch(_ process: Process) throws -> Bool {
        lock.lock(); defer { lock.unlock() }
        guard !cancelled else { return false }
        try process.run()
        self.process = process
        return true
    }

    func cancel() {
        lock.lock(); defer { lock.unlock() }
        cancelled = true
        if let process, process.isRunning { process.terminate() }
    }
}

private struct Benchmark: Identifiable {
    let id = UUID()
    let platform: String
    let title: String
    let url: String
    let creator: String
    let views: String

    init(_ value: [String: Any]) {
        platform = value["platform"] as? String ?? ""
        title = value["title"] as? String ?? ""
        url = value["url"] as? String ?? ""
        creator = value["creator"] as? String ?? ""
        if let number = value["views"] as? NSNumber { views = number.stringValue }
        else { views = "" }
    }
}

@MainActor
private final class ToolModel: ObservableObject {
    @Published var page: ToolPage = .video
    @Published var sourceVideo: URL?
    @Published var workspace = ""
    @Published var internalSpace = "检查中"
    @Published var externalSpace = "检查中"
    @Published var mediaAllowed = false
    @Published var mediaReason = "正在检查磁盘和运行时"
    @Published var busy = false
    @Published var message = ""
    @Published var query = ""
    @Published var searchPlatform = "bilibili"
    @Published var benchmarks: [Benchmark] = []
    @Published var douyinURL = ""
    @Published var douyinTitle = ""
    @Published var assetOrigin = "muse"
    @Published var rightsNote = "用户已声明本批素材可商用；具体授权范围待关联"
    @Published var assets: [[String: Any]] = []
    @Published var assetFilter = ""
    @Published var selectedAssetIDs: [String] = []
    @Published var shotName = ""
    @Published var shotManifest = ""
    @Published var phase = ""
    @Published var startedAt: Date?
    @Published var workspaceInfo: [String: Any] = [:]
    @Published var recentWorkspaces = UserDefaults.standard.stringArray(forKey: "recentWorkspaces") ?? []
    @Published var provider = ""
    @Published var translationModel = ""
    @Published var effort = ""
    @Published var reviewConfirmed = false
    private var activeControl: CommandControl?

    init() {
        workspace = UserDefaults.standard.string(forKey: "lastWorkspace") ?? ""
        let recovery = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/出海视频/last_task.json")
        if let data = try? Data(contentsOf: recovery),
           let task = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           let path = task["workspace"] as? String {
            let selectedAt = UserDefaults.standard.double(forKey: "lastWorkspaceSelectedAt")
            let taskAt = task["updated_at"] as? Double ?? 0
            if workspace.isEmpty || taskAt > selectedAt { workspace = path }
        }
    }

    var visibleAssets: [[String: Any]] {
        let needle = assetFilter.trimmingCharacters(in: .whitespacesAndNewlines)
        if needle.isEmpty { return assets }
        return assets.filter { card in
            let fields = [card["material_id"] as? String ?? "",
                          card["display_name"] as? String ?? ""]
                + (card["tags"] as? [String] ?? []) + (card["shot_roles"] as? [String] ?? [])
            return fields.joined(separator: " ").localizedCaseInsensitiveContains(needle)
        }
    }

    private func command(_ args: [String], completion: @escaping ([String: Any]) -> Void) {
        guard !busy else { return }
        guard let resource = Bundle.main.url(forResource: "video_tool", withExtension: "py") else {
            message = "程序文件不完整，请重新安装"; return
        }
        busy = true
        phase = "正在开始…"
        startedAt = Date()
        let process = Process()
        let control = CommandControl()
        activeControl = control
        DispatchQueue.global(qos: .userInitiated).async {
            process.executableURL = URL(fileURLWithPath: "/Users/macmini/.local/share/uv/python/cpython-3.11.15-macos-aarch64-none/bin/python3.11")
            process.arguments = [resource.path] + args
            process.currentDirectoryURL = Bundle.main.resourceURL
            let output = Pipe()
            let events = Pipe()
            process.standardOutput = output
            process.standardError = events
            var result: [String: Any] = ["ok": false, "error": "命令未运行"]
            do {
                guard try control.launch(process) else {
                    DispatchQueue.main.async {
                        self.busy = false; self.activeControl = nil
                        self.phase = ""; self.startedAt = nil
                        completion(["ok": false, "error": "任务已停止，未启动制作"])
                    }
                    return
                }
                DispatchQueue.global(qos: .utility).async {
                    var pending = Data()
                    while true {
                        let data = events.fileHandleForReading.availableData
                        if data.isEmpty { break }
                        pending.append(data)
                        while let newline = pending.firstIndex(of: 10) {
                            let line = pending.prefix(upTo: newline)
                            pending.removeSubrange(...newline)
                            if let event = try? JSONSerialization.jsonObject(with: line) as? [String: Any],
                               let progress = event["progress"] as? String {
                                DispatchQueue.main.async { self.phase = progress }
                            }
                        }
                        if pending.count > 32_768 { pending.removeAll() }
                    }
                }
                let longTask = args.first == "prepare-video" || (args.first == "workspace" && ["prepare", "media"].contains(args.dropFirst().first ?? ""))
                let limit: TimeInterval = longTask ? 14_400 : (args.first == "import-muse-batch" ? 300 : 180)
                DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + limit) {
                    control.cancel()
                }
                let bytes = output.fileHandleForReading.readDataToEndOfFile()
                process.waitUntilExit()
                let text = String(data: bytes, encoding: .utf8) ?? ""
                if let data = text.data(using: .utf8),
                   let parsed = try JSONSerialization.jsonObject(with: data) as? [String: Any] {
                    result = parsed
                } else {
                    result = ["ok": false, "error": process.terminationStatus == 130 ? "任务已停止，工作区和已完成步骤保留，可继续" : "任务中断或返回异常：\(text.prefix(300))"]
                }
            } catch {
                result = ["ok": false, "error": error.localizedDescription]
            }
            DispatchQueue.main.async {
                self.busy = false
                self.activeControl = nil
                self.phase = ""
                self.startedAt = nil
                completion(result)
            }
        }
    }

    func cancel() {
        guard let activeControl else { return }
        activeControl.cancel(); phase = "正在停止本次任务，保留已完成步骤…"
    }

    func rememberWorkspace(_ path: String) {
        workspace = path
        recentWorkspaces.removeAll { $0 == path }
        recentWorkspaces.insert(path, at: 0)
        recentWorkspaces = Array(recentWorkspaces.prefix(20))
        UserDefaults.standard.set(path, forKey: "lastWorkspace")
        UserDefaults.standard.set(Date().timeIntervalSince1970, forKey: "lastWorkspaceSelectedAt")
        UserDefaults.standard.set(recentWorkspaces, forKey: "recentWorkspaces")
    }

    func openWorkspace() {
        guard !busy else { return }
        let panel = NSOpenPanel()
        panel.allowedContentTypes = [.folder]
        panel.canChooseFiles = false; panel.canChooseDirectories = true
        panel.allowsMultipleSelection = false
        panel.message = "选择已有 Stable3 工作区（包含 state.json）"
        panel.directoryURL = URL(fileURLWithPath: "/Volumes/Obsidian知识库/Codex工作运行/10_视频进行中")
        if panel.runModal() == .OK, let url = panel.url { loadWorkspace(url.path) }
    }

    func loadWorkspace(_ path: String) {
        command(["workspace", "resume", path]) { response in
            self.acceptWorkspace(response)
        }
    }

    private func acceptWorkspace(_ response: [String: Any]) {
        guard let data = response["data"] as? [String: Any], let path = data["workspace"] as? String else {
            message = response["error"] as? String ?? "读取工作区失败"; return
        }
        workspaceInfo = data
        rememberWorkspace(path)
        reviewConfirmed = false
        let provenance = data["provenance"] as? [String: Any] ?? [:]
        provider = provenance["provider"] as? String ?? ""
        translationModel = provenance["model"] as? String ?? ""
        effort = provenance["reasoning_effort"] as? String ?? ""
        message = data["finished"] as? Bool == true ? "已有成片，可直接预览视频和字幕" : "工作区已恢复；继续尚未完成的步骤"
    }

    func workspaceAction(_ action: String) {
        var args = ["workspace", action, workspace]
        if action == "approve" {
            guard reviewConfirmed else { return }
            args += ["--provider", provider, "--model", translationModel, "--effort", effort,
                     "--reviewed-sha", workspaceInfo["review_sha256"] as? String ?? "", "--confirm"]
        }
        command(args) { response in self.acceptWorkspace(response) }
    }

    func openCreator() {
        NSWorkspace.shared.open(URL(fileURLWithPath: "/Users/macmini/Applications/口播制作.app"))
    }

    func sendShot() {
        command(["send-shot"] + selectedAssetIDs + ["--name", shotName]) { response in
            if response["ok"] as? Bool == true {
                self.shotManifest = (response["data"] as? [String: Any])?["manifest_path"] as? String ?? ""
                self.message = "已发送选材，原件仍在外盘"
                self.openCreator()
            } else { self.message = response["error"] as? String ?? "发送失败" }
        }
    }

    func editAsset(_ card: [String: Any], name: String, tags: String) {
        command(["edit-asset", card["material_id"] as? String ?? "", name, tags]) { response in
            if response["ok"] as? Bool == true { self.loadAssets() }
            else { self.message = response["error"] as? String ?? "保存失败" }
        }
    }

    func doctor() {
        command(["doctor"]) { response in
            if let data = response["data"] as? [String: Any] {
                self.internalSpace = "\(data["internal_free_gib"] ?? "?") GiB"
                self.externalSpace = "\(data["external_free_gib"] ?? "?") GiB"
                self.mediaAllowed = data["can_run_media"] as? Bool ?? false
                self.mediaReason = data["media_block_reason"] as? String ?? ""
                self.message = self.mediaAllowed ? "外盘与磁盘预检通过" : self.mediaReason
                if !self.workspace.isEmpty && self.workspaceInfo.isEmpty { self.loadWorkspace(self.workspace) }
            } else {
                self.message = response["error"] as? String ?? "预检失败"
                self.mediaReason = self.message
            }
        }
    }

    func selectVideo() {
        let panel = NSOpenPanel()
        panel.allowedContentTypes = [.movie, .mpeg4Movie, .quickTimeMovie]
        panel.canChooseDirectories = false
        if panel.runModal() == .OK { sourceVideo = panel.url }
    }

    func prepareVideo() {
        guard let sourceVideo else { return }
        command(["prepare-video", sourceVideo.path]) { response in
            if let data = response["data"] as? [String: Any] {
                self.workspace = data["workspace"] as? String ?? ""
                self.rememberWorkspace(self.workspace)
                self.loadWorkspace(self.workspace)
            } else { self.message = response["error"] as? String ?? "准备失败" }
        }
    }

    func search() {
        guard !busy, !query.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return }
        command(["search", searchPlatform, query]) { response in
            if let data = response["data"] as? [String: Any],
               let values = data["results"] as? [[String: Any]] {
                self.benchmarks = values.map(Benchmark.init)
                self.message = "找到 \(values.count) 条线索；尚未审片或核权"
            } else { self.message = response["error"] as? String ?? "搜索失败" }
        }
    }

    func save(_ item: Benchmark) {
        command(["save-benchmark", item.platform, item.url, item.title,
                 "--creator", item.creator]) { response in
            self.message = (response["ok"] as? Bool ?? false) ? "已保存对标线索，未下载原视频" :
                (response["error"] as? String ?? "保存失败")
        }
    }

    func saveDouyin() {
        command(["save-benchmark", "douyin", douyinURL, douyinTitle]) { response in
            self.message = (response["ok"] as? Bool ?? false) ? "已保存抖音链接线索" :
                (response["error"] as? String ?? "保存失败")
        }
    }

    func importAsset() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = false
        panel.allowedContentTypes = [.movie, .image]
        guard panel.runModal() == .OK, let url = panel.url else { return }
        command(["import-asset", url.path, assetOrigin, "--rights-note", rightsNote]) { response in
            if let data = response["data"] as? [String: Any] {
                self.message = "已导入外盘候选：\(data["asset_path"] ?? "")"
                self.loadAssets()
            } else { self.message = response["error"] as? String ?? "导入失败" }
        }
    }

    func importMuseBatch() {
        guard !busy else { return }
        let panel = NSOpenPanel()
        panel.canChooseDirectories = false
        panel.allowedContentTypes = [.zip]
        panel.message = "选择已下载到外盘的 MUSE 素材包（包含 INDEX.json）"
        panel.directoryURL = URL(fileURLWithPath: "/Volumes/Obsidian知识库/Codex工作运行/30_工作过程素材/视频出海工具素材库/MUSE候选")
        guard panel.runModal() == .OK, let url = panel.url else { return }
        command(["import-muse-batch", url.path]) { response in
            if response["ok"] as? Bool == true {
                self.loadAssets()
            } else { self.message = response["error"] as? String ?? "素材包接收失败；保留原包" }
        }
    }

    func loadAssets() {
        command(["list-assets"]) { response in
            if let data = response["data"] as? [String: Any] {
                self.assets = data["assets"] as? [[String: Any]] ?? []
                let available = Set(self.assets.compactMap { $0["material_id"] as? String })
                self.selectedAssetIDs.removeAll { !available.contains($0) }
                self.message = "外盘候选素材 \(self.assets.count) 条"
            } else { self.message = response["error"] as? String ?? "读取素材库失败" }
        }
    }

    func toggleAsset(_ id: String) {
        guard !id.isEmpty else { return }
        if let index = selectedAssetIDs.firstIndex(of: id) {
            selectedAssetIDs.remove(at: index)
        } else if selectedAssetIDs.count < 2 {
            selectedAssetIDs.append(id)
        } else {
            message = "一个镜头最多选 2 件素材；先取消一件再选择"
        }
    }

    func prepareShot() {
        guard !selectedAssetIDs.isEmpty else { return }
        command(["prepare-shot"] + selectedAssetIDs + ["--name", shotName]) { response in
            if let data = response["data"] as? [String: Any] {
                self.shotManifest = data["manifest_path"] as? String ?? ""
                self.message = "已生成镜头取用清单；直接引用外盘原件，没有复制或下载媒体"
            } else { self.message = response["error"] as? String ?? "取用失败" }
        }
    }
}

private enum ToolSheet: Identifiable {
    case media(MediaSelection)
    case asset(AssetEditSelection)
    var id: UUID {
        switch self {
        case .media(let selection): return selection.id
        case .asset(let selection): return selection.id
        }
    }
}

private struct ContentView: View {
    @StateObject private var model = ToolModel()
    private let ink = Color(red: 0.10, green: 0.15, blue: 0.24)
    private let blue = Color(red: 0.14, green: 0.34, blue: 0.83)
    @State private var activeSheet: ToolSheet?

    var body: some View {
        HStack(spacing: 0) {
            VStack(alignment: .leading, spacing: 0) {
                HStack(spacing: 9) {
                    Image(systemName: "rectangle.3.group")
                        .font(.system(size: 23, weight: .medium))
                        .foregroundStyle(.white)
                    Text("出海视频")
                        .font(.system(size: 21, weight: .semibold))
                        .foregroundStyle(.white)
                }
                .padding(.bottom, 40)
                ForEach(ToolPage.allCases) { page in
                    Button {
                        model.page = page
                        if page == .assets && !model.busy { model.loadAssets() }
                        if page == .video && !model.busy { model.doctor() }
                    } label: {
                        HStack(spacing: 12) {
                            Image(systemName: page.symbol).frame(width: 20)
                            Text(page.rawValue)
                            Spacer()
                        }
                        .font(.system(size: 15, weight: .medium))
                        .padding(.vertical, 12)
                        .padding(.horizontal, 13)
                        .background(model.page == page ? Color.white.opacity(0.17) : .clear,
                                    in: RoundedRectangle(cornerRadius: 8))
                    }
                    .buttonStyle(.plain)
                    .foregroundStyle(.white)
                    .padding(.bottom, 4)
                }
                Button("没有源片？制作中文视频") { model.openCreator() }
                    .buttonStyle(.plain).foregroundStyle(.white.opacity(0.85))
                    .font(.system(size: 12)).padding(.top, 26)
                Spacer()
                Text("本机工具 · 媒体存外盘")
                    .font(.system(size: 11))
                    .foregroundStyle(.white.opacity(0.65))
            }
            .padding(22)
            .frame(width: 190)
            .background(ink)

            VStack(alignment: .leading, spacing: 0) {
                ScrollView {
                    VStack(alignment: .leading, spacing: 22) {
                        switch model.page {
                        case .video: videoPage
                        case .research: researchPage
                        case .assets: assetsPage
                        }
                    }
                    .padding(36)
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
                Divider()
                HStack(spacing: 8) {
                    if model.busy { ProgressView().controlSize(.small) }
                    Text(model.busy ? model.phase : (model.message.isEmpty ? "选择一项任务开始" : model.message))
                        .font(.system(size: 12))
                        .lineLimit(2)
                    Spacer()
                    if let start = model.startedAt {
                        Text(start, style: .timer).font(.system(size: 12)).monospacedDigit()
                        Button("停止任务") { model.cancel() }
                    }
                }
                .padding(.horizontal, 22)
                .frame(height: 48)
            }
            .background(Color(nsColor: .windowBackgroundColor))
        }
        .frame(minWidth: 800, minHeight: 580)
        .onAppear { model.doctor() }
        .onReceive(NotificationCenter.default.publisher(for: NSApplication.willTerminateNotification)) { _ in model.cancel() }
        .onReceive(NotificationCenter.default.publisher(for: NSApplication.didBecomeActiveNotification)) { _ in
            if !model.busy { model.doctor() }
        }
        .sheet(item: $activeSheet) { sheet in
            switch sheet {
            case .media(let selection): MediaPreview(selection: selection)
            case .asset(let item):
                AssetEditForm(item: item) { name, tags in
                    model.editAsset(item.card, name: name, tags: tags)
                }
            }
        }
    }

    private var videoPage: some View {
        VStack(alignment: .leading, spacing: 20) {
            title("把中文视频带给海外观众", subtitle: "选择源片，或接着完成上次的视频。英文稿确认后再制作。")
            HStack {
                Label("内置盘剩余 \(model.internalSpace)", systemImage: "internaldrive")
                Spacer()
                Label("外接盘剩余 \(model.externalSpace)", systemImage: "externaldrive")
                Button { model.doctor() } label: { Image(systemName: "arrow.clockwise") }
                    .help("刷新磁盘与运行时状态").disabled(model.busy)
            }
            .font(.system(size: 13))
            .padding(16)
            .background(.quaternary.opacity(0.45), in: RoundedRectangle(cornerRadius: 9))
            if !model.mediaAllowed {
                Label(model.mediaReason, systemImage: "pause.circle")
                    .font(.system(size: 14, weight: .medium))
                    .foregroundStyle(.orange)
            }
            Text("源视频")
                .font(.system(size: 13, weight: .semibold))
            HStack {
                Text(model.sourceVideo?.path ?? "尚未选择")
                    .lineLimit(2).textSelection(.enabled)
                    .foregroundStyle(model.sourceVideo == nil ? .secondary : .primary)
                Spacer()
                Button("选择视频…") { model.selectVideo() }.disabled(model.busy)
            }
            .padding(15)
            .overlay(RoundedRectangle(cornerRadius: 8).stroke(.gray.opacity(0.35)))
            Button("检查源片并建立独立工作区") { model.prepareVideo() }
                .buttonStyle(.borderedProminent)
                .tint(blue)
                .disabled(model.busy || !model.mediaAllowed || model.sourceVideo == nil)
            HStack {
                Button("打开已有工作区…") { model.openWorkspace() }.disabled(model.busy)
                if !model.recentWorkspaces.isEmpty {
                    Menu("最近的视频") {
                        ForEach(model.recentWorkspaces, id: \.self) { path in
                            Button(URL(fileURLWithPath: path).deletingLastPathComponent().lastPathComponent) { model.loadWorkspace(path) }
                        }
                    }.disabled(model.busy)
                }
            }
            if !model.workspaceInfo.isEmpty { workspacePanel }
        }
    }

    private var workspacePanel: some View {
        let data = model.workspaceInfo
        let report = data["report"] as? [String: Any] ?? [:]
        let finished = data["finished"] as? Bool == true
        let approved = data["approved"] as? Bool == true
        let script = data["script"] as? String ?? ""
        return VStack(alignment: .leading, spacing: 16) {
            Divider()
            HStack {
                Text(data["title"] as? String ?? "本期视频").font(.system(size: 19, weight: .semibold))
                Spacer()
                Button("刷新进度") { model.loadWorkspace(model.workspace) }.disabled(model.busy)
                Button("资料文件夹") { NSWorkspace.shared.open(URL(fileURLWithPath: model.workspace)) }
            }
            Text(verbatim: finished ? "自动媒体报告通过 · 完整视听待你确认" : "准备：\(stageLabel(data["prepare"]))    英文稿：\(stageLabel(data["translation"]))    媒体：\(stageLabel(data["media"]))")
                .font(.system(size: 13)).foregroundStyle(finished ? .green : .secondary)
            if finished {
                HStack {
                    Button("预览英文成片") { activeSheet = .media(MediaSelection(path: data["final_path"] as? String ?? "")) }
                        .buttonStyle(.borderedProminent)
                    Button("打开英文字幕") { NSWorkspace.shared.open(URL(fileURLWithPath: model.workspace + "/full_en.srt")) }
                    Button("显示交付视频") { NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: data["final_path"] as? String ?? "")]) }
                }
            } else {
                HStack {
                    if data["prepare"] as? String != "done" {
                        Button("继续源片准备") { model.workspaceAction("prepare") }.disabled(model.busy || !model.mediaAllowed)
                    }
                    Button("验证英文稿") { model.workspaceAction("validate") }.disabled(model.busy)
                    Button("查看分段稿") { NSWorkspace.shared.open(URL(fileURLWithPath: model.workspace + "/segments_translated.json")) }
                    Button("继续制作英文视频") { model.workspaceAction("media") }
                        .disabled(model.busy || !approved || !model.mediaAllowed)
                }
            }
            if !script.isEmpty {
                DisclosureGroup("完整英文稿与验证报告") {
                    VStack(alignment: .leading, spacing: 12) {
                        ScrollView { Text(script).font(.system(size: 14)).textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading) }
                            .frame(height: 240).padding(12).background(.background, in: RoundedRectangle(cornerRadius: 6))
                        Text(verbatim: "自动验证：\(report["passed"] as? Bool == true ? "通过" : "尚未通过")    错误 \(report["error_count"] ?? "?")    待核对项 \(report["warning_count"] ?? "?")")
                        if let warnings = report["warnings"] as? [[String: Any]] {
                            ForEach(Array(warnings.enumerated()), id: \.offset) { _, warning in
                                Text(verbatim: "第 \(warning["segment"] ?? "?") 段：\(warning["message"] ?? "")").font(.system(size: 12)).textSelection(.enabled)
                            }
                        }
                        Button("打开证据报告") { NSWorkspace.shared.open(URL(fileURLWithPath: model.workspace + "/evidence_ledger.json")) }
                        if !approved && !finished {
                            Text("翻译实际运行记录（不代表当前助手身份）").font(.system(size: 12)).foregroundStyle(.secondary)
                            HStack {
                                TextField("Provider", text: $model.provider)
                                TextField("准确模型 ID", text: $model.translationModel)
                                TextField("推理档位", text: $model.effort)
                            }.textFieldStyle(.roundedBorder)
                            Toggle("我已核对完整英文稿、数字、实体与报告中的待核对项", isOn: $model.reviewConfirmed)
                            Button("批准英文稿") { model.workspaceAction("approve") }
                                .disabled(model.busy || !model.reviewConfirmed || report["passed"] as? Bool != true || model.provider.isEmpty || model.translationModel.isEmpty || model.effort.isEmpty)
                        }
                    }.padding(.top, 10)
                }
            } else {
                Text("源片准备后，需要按固定翻译流程起草英文稿；本版不会编造译稿或跳过批准。").font(.system(size: 12)).foregroundStyle(.secondary)
            }
        }
    }

    private func stageLabel(_ value: Any?) -> String {
        let raw = value as? String ?? "待开始"
        switch raw {
        case "done": return "完成"
        case "pending": return "待开始"
        case "review_required": return "待确认"
        case "failed_quality_gate": return "需修正"
        case "running": return "进行中"
        default: return raw
        }
    }

    private var researchPage: some View {
        VStack(alignment: .leading, spacing: 20) {
            title("找对标", subtitle: "找故事结构和表达方式；公开视频先保存链接，不直接拿画面入片。")
            HStack {
                Picker("平台", selection: $model.searchPlatform) {
                    Text("B站").tag("bilibili")
                    Text("YouTube").tag("youtube")
                }
                .frame(width: 150)
                TextField("输入产品、问题或用户场景", text: $model.query)
                    .textFieldStyle(.roundedBorder)
                    .onSubmit { model.search() }
                Button("搜索") { model.search() }
                    .disabled(model.busy || model.query.trimmingCharacters(in: .whitespaces).isEmpty)
            }
            ForEach(model.benchmarks) { item in
                HStack(alignment: .top, spacing: 14) {
                    VStack(alignment: .leading, spacing: 6) {
                        Text(item.title).font(.system(size: 14, weight: .semibold))
                        Text("\(item.creator)  \(item.views.isEmpty ? "" : "· \(item.views) 次播放")")
                            .font(.system(size: 12)).foregroundStyle(.secondary)
                        Text(item.url).font(.system(size: 11)).foregroundStyle(blue)
                            .textSelection(.enabled)
                    }
                    Spacer()
                    Button("收录线索") { model.save(item) }.disabled(model.busy)
                }
                .padding(.vertical, 9)
                Divider()
            }
            VStack(alignment: .leading, spacing: 9) {
                Text("抖音链接")
                    .font(.system(size: 13, weight: .semibold))
                Text("目前先保存人工找到的公开链接；没有可靠来源时不批量抓取。")
                    .font(.system(size: 12)).foregroundStyle(.secondary)
                TextField("https://www.douyin.com/video/…", text: $model.douyinURL)
                TextField("视频标题", text: $model.douyinTitle)
                Button("保存抖音线索") { model.saveDouyin() }
                    .disabled(model.busy || model.douyinURL.isEmpty || model.douyinTitle.isEmpty)
            }
            .textFieldStyle(.roundedBorder)
            .padding(.top, 6)
        }
    }

    private var assetsPage: some View {
        VStack(alignment: .leading, spacing: 20) {
            title("为这个镜头选画面", subtitle: "先预览，再选择。原件保留外盘，不制造内盘副本。")
            HStack {
                Picker("来源", selection: $model.assetOrigin) {
                    Text("MUSE 生成").tag("muse")
                    Text("本人录屏").tag("self_recorded")
                    Text("第三方授权").tag("licensed_external")
                }
                .frame(width: 220)
                Button("导入图片或视频…") { model.importAsset() }
                    .buttonStyle(.borderedProminent).tint(blue).disabled(model.busy)
                Button("接收 MUSE 素材包…") { model.importMuseBatch() }.disabled(model.busy)
                Button("刷新") { model.loadAssets() }.disabled(model.busy)
            }
            DisclosureGroup("素材批次说明") {
                TextField("本批来源或使用说明", text: $model.rightsNote).textFieldStyle(.roundedBorder)
            }.font(.system(size: 12))
            HStack {
                TextField("搜索名称、镜头用途或标签", text: $model.assetFilter)
                    .textFieldStyle(.roundedBorder)
                Text("已选 \(model.selectedAssetIDs.count)/2")
                    .font(.system(size: 12)).foregroundStyle(.secondary)
            }
            HStack {
                TextField("本镜头名称（可选）", text: $model.shotName)
                    .textFieldStyle(.roundedBorder)
                Button("生成取用清单") { model.prepareShot() }
                    .disabled(model.busy || model.selectedAssetIDs.isEmpty)
                Button("交给口播制作") { model.sendShot() }
                    .buttonStyle(.borderedProminent).tint(blue)
                    .disabled(model.busy || model.selectedAssetIDs.isEmpty)
            }
            if !model.shotManifest.isEmpty {
                Text("清单：\(model.shotManifest)")
                    .font(.system(size: 11)).textSelection(.enabled)
            }
            LazyVGrid(columns: [GridItem(.adaptive(minimum: 260), spacing: 18)], alignment: .leading, spacing: 20) {
              ForEach(Array(model.visibleAssets.enumerated()), id: \.offset) { _, card in
                VStack(alignment: .leading, spacing: 10) {
                    Button { activeSheet = .media(MediaSelection(path: card["path"] as? String ?? "")) } label: {
                        AssetThumbnail(path: card["path"] as? String ?? "")
                            .aspectRatio(16 / 9, contentMode: .fit)
                    }.buttonStyle(.plain).help("预览素材")
                    VStack(alignment: .leading, spacing: 4) {
                        Text(card["display_name"] as? String ?? URL(fileURLWithPath: card["path"] as? String ?? "").lastPathComponent)
                            .font(.system(size: 13, weight: .medium))
                        Text((card["tags"] as? [String] ?? []).joined(separator: " / "))
                            .font(.system(size: 11)).foregroundStyle(.secondary).textSelection(.enabled)
                        Text("外盘原件    云端\(cloudLabel(card))")
                            .font(.system(size: 11)).foregroundStyle(.secondary)
                        if card["needs_revision"] as? Bool == true {
                            Text("画面有问题 · 等待修正版").font(.system(size: 11)).foregroundStyle(.orange)
                        }
                        if let check = card["agent_visual_check"] as? String {
                            DisclosureGroup("画面检查") { Text(check).font(.system(size: 11)).textSelection(.enabled) }
                        }
                    }
                    HStack {
                    Button("名称与标签") { activeSheet = .asset(AssetEditSelection(card: card)) }.disabled(model.busy)
                    Spacer()
                    Button(model.selectedAssetIDs.contains(card["material_id"] as? String ?? "") ? "已选中" : "选用") {
                        model.toggleAsset(card["material_id"] as? String ?? "")
                    }.disabled(model.busy || card["needs_revision"] as? Bool == true)
                    }
                }
                .padding(10).background(.background, in: RoundedRectangle(cornerRadius: 8))
                .overlay(RoundedRectangle(cornerRadius: 8).stroke(model.selectedAssetIDs.contains(card["material_id"] as? String ?? "") ? blue : .clear, lineWidth: 2))
              }
            }
            if !model.assets.isEmpty && model.visibleAssets.isEmpty { Text("没有匹配素材，试试镜头用途或更短的关键词。").foregroundStyle(.secondary) }
            if model.assets.isEmpty {
                Text("还没有入库素材。先导入文件，系统会建立素材 ID 和候选卡。")
                    .font(.system(size: 13)).foregroundStyle(.secondary)
            }
        }
    }

    private func cloudLabel(_ card: [String: Any]) -> String {
        let status = (card["cloud"] as? [String: Any])?["status"] as? String ?? "not_uploaded"
        switch status {
        case "not_uploaded": return "未上传"
        case "verified_remote": return "远端已校验"
        default: return status
        }
    }

    private func title(_ text: String, subtitle: String) -> some View {
        VStack(alignment: .leading, spacing: 9) {
            Text(text).font(.system(size: 30, weight: .bold, design: .rounded))
                .foregroundStyle(.primary)
            Text(subtitle).font(.system(size: 14)).foregroundStyle(.secondary)
        }
        .padding(.bottom, 7)
    }
}

@main
struct CreatorVideoApp: App {
    var body: some Scene {
        WindowGroup { ContentView() }
            .windowStyle(.titleBar)
    }
}
