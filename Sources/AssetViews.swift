import SwiftUI
import AppKit
import AVKit
import ImageIO

struct MediaSelection: Identifiable {
    let id = UUID()
    let path: String
}

struct AssetEditSelection: Identifiable {
    let id = UUID()
    let card: [String: Any]
}

struct AssetThumbnail: View {
    let path: String
    var maximumPixelSize = 480
    @State private var image: NSImage?
    @State private var failed = false
    private static let cache: NSCache<NSString, NSImage> = {
        let cache = NSCache<NSString, NSImage>()
        cache.countLimit = 60; cache.totalCostLimit = 24 * 1024 * 1024
        return cache
    }()

    var body: some View {
        ZStack(alignment: .bottomTrailing) {
            Color(nsColor: .controlBackgroundColor)
            if let image { Image(nsImage: image).resizable().scaledToFit().frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .center) }
            else { Image(systemName: failed ? "exclamationmark.triangle" : "photo").font(.system(size: 24)).foregroundStyle(.secondary) }
            if ["mp4", "mov", "m4v"].contains(URL(fileURLWithPath: path).pathExtension.lowercased()) {
                Image(systemName: "play.fill").font(.system(size: 13)).padding(8).background(.regularMaterial, in: Circle()).padding(8)
            }
        }.clipped().task(id: path) { load() }
    }

    private func load() {
        let key = "\(path)-\(maximumPixelSize)" as NSString
        if let cached = Self.cache.object(forKey: key) { image = cached; return }
        let url = URL(fileURLWithPath: path)
        guard FileManager.default.fileExists(atPath: path) else { failed = true; return }
        DispatchQueue.global(qos: .utility).async {
            if ["mp4", "mov", "m4v"].contains(url.pathExtension.lowercased()) {
                let generator = AVAssetImageGenerator(asset: AVURLAsset(url: url))
                generator.appliesPreferredTrackTransform = true
                generator.maximumSize = CGSize(width: maximumPixelSize, height: maximumPixelSize)
                generator.generateCGImagesAsynchronously(forTimes: [NSValue(time: .zero)]) { _, cgImage, _, _, _ in
                    accept(cgImage)
                }
            } else {
                let source = CGImageSourceCreateWithURL(url as CFURL, nil)
                let options: [CFString: Any] = [kCGImageSourceCreateThumbnailFromImageAlways: true,
                    kCGImageSourceThumbnailMaxPixelSize: maximumPixelSize, kCGImageSourceCreateThumbnailWithTransform: true]
                accept(source.flatMap { CGImageSourceCreateThumbnailAtIndex($0, 0, options as CFDictionary) })
            }
        }
    }

    private func accept(_ cgImage: CGImage?) {
        DispatchQueue.main.async {
            if let cgImage {
                let thumbnail = NSImage(cgImage: cgImage, size: .zero)
                Self.cache.setObject(thumbnail, forKey: "\(path)-\(maximumPixelSize)" as NSString, cost: cgImage.width * cgImage.height * 4)
                image = thumbnail
            } else { failed = true }
        }
    }
}

struct NativeMovieView: NSViewRepresentable {
    let path: String
    func makeNSView(context: Context) -> AVPlayerView {
        let view = AVPlayerView()
        view.controlsStyle = .inline
        view.videoGravity = .resizeAspect
        view.player = AVPlayer(url: URL(fileURLWithPath: path))
        return view
    }
    func updateNSView(_ view: AVPlayerView, context: Context) {}
    static func dismantleNSView(_ view: AVPlayerView, coordinator: ()) {
        view.player?.pause()
        view.player = nil
    }
}

struct MediaPreview: View {
    let selection: MediaSelection
    @Environment(\.dismiss) private var dismiss

    private var isVideo: Bool { ["mp4", "mov", "m4v"].contains(URL(fileURLWithPath: selection.path).pathExtension.lowercased()) }
    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack {
                Text(URL(fileURLWithPath: selection.path).lastPathComponent).font(.headline).lineLimit(1)
                Spacer(); Button("关闭") { dismiss() }.keyboardShortcut(.cancelAction)
            }
            if !FileManager.default.fileExists(atPath: selection.path) {
                Text("原件当前不可用，请连接外盘。云端下载尚未配置。")
            } else if isVideo {
                NativeMovieView(path: selection.path).frame(minWidth: 640, minHeight: 360)
            } else {
                AssetThumbnail(path: selection.path, maximumPixelSize: 1920).frame(width: 720, height: 405)
            }
            Button("在 Finder 显示原件") { NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: selection.path)]) }
        }.padding(22)
    }
}

struct AssetEditForm: View {
    let item: AssetEditSelection
    let save: (String, String) -> Void
    @State private var name: String
    @State private var tags: String
    @Environment(\.dismiss) private var dismiss
    init(item: AssetEditSelection, save: @escaping (String, String) -> Void) {
        self.item = item; self.save = save
        _name = State(initialValue: item.card["display_name"] as? String ?? "")
        _tags = State(initialValue: (item.card["tags"] as? [String] ?? []).joined(separator: "，"))
    }
    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("名称与标签").font(.title2.weight(.semibold))
            TextField("素材名称", text: $name)
            TextField("标签，用逗号分开", text: $tags)
            Text("只修改目录信息，不改动原件。").font(.caption).foregroundStyle(.secondary)
            HStack { Spacer(); Button("取消") { dismiss() }; Button("保存") { save(name, tags); dismiss() }.disabled(name.trimmingCharacters(in: .whitespaces).isEmpty) }
        }.textFieldStyle(.roundedBorder).padding(24).frame(width: 480)
    }
}
