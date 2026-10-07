// syscap — macOS 시스템 오디오 캡처 도우미 (ScreenCaptureKit, macOS 13+)
// 스피커/이어폰으로 나오는 모든 소리를 16 kHz mono float32 little-endian PCM 으로 stdout 에 쓴다.
// 상태는 stderr 로 한 줄씩: READY / ERR_PERMISSION / ERR <메시지>
// 부모 프로세스(stdin)가 닫히면 스스로 종료한다.
import Foundation
import ScreenCaptureKit
import CoreMedia
import AVFoundation

setvbuf(stdout, nil, _IONBF, 0)
let out = FileHandle.standardOutput

func log(_ s: String) {
    FileHandle.standardError.write((s + "\n").data(using: .utf8)!)
}

final class Capturer: NSObject, SCStreamOutput, SCStreamDelegate {
    var stream: SCStream?
    let queue = DispatchQueue(label: "syscap.audio")

    func start() async throws {
        let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: true)
        guard let display = content.displays.first else { throw NSError(domain: "syscap", code: 1, userInfo: [NSLocalizedDescriptionKey: "no display"]) }
        let filter = SCContentFilter(display: display, excludingApplications: [], exceptingWindows: [])
        let cfg = SCStreamConfiguration()
        cfg.capturesAudio = true
        cfg.sampleRate = 16000           // VAD/전사에 맞춰 바로 16k mono → 리샘플 불필요
        cfg.channelCount = 1
        cfg.excludesCurrentProcessAudio = true
        // 영상은 쓰지 않으므로 최소 크기·최저 프레임레이트
        cfg.width = 2
        cfg.height = 2
        cfg.minimumFrameInterval = CMTime(value: 1, timescale: 1)
        cfg.queueDepth = 3
        let s = SCStream(filter: filter, configuration: cfg, delegate: self)
        try s.addStreamOutput(self, type: .audio, sampleHandlerQueue: queue)
        try s.addStreamOutput(self, type: .screen, sampleHandlerQueue: DispatchQueue(label: "syscap.video"))
        try await s.startCapture()
        stream = s
    }

    func stream(_ stream: SCStream, didOutputSampleBuffer sb: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .audio, sb.isValid else { return }
        do {
            try sb.withAudioBufferList { abl, _ in
                // mono 설정이므로 첫 버퍼만 사용 (비인터리브 float32)
                guard let buf = abl.first, let data = buf.mData else { return }
                out.write(Data(bytes: data, count: Int(buf.mDataByteSize)))
            }
        } catch {
            // 일시적 포맷 오류는 무시
        }
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        log("ERR stopped: \(error.localizedDescription)")
        exit(3)
    }
}

// 부모가 사라지면 종료
Thread.detachNewThread {
    while true {
        let d = FileHandle.standardInput.availableData
        if d.isEmpty { exit(0) }
    }
}
signal(SIGPIPE) { _ in exit(0) }

let cap = Capturer()
Task {
    do {
        try await cap.start()
        log("READY")
    } catch {
        let ns = error as NSError
        // SCStreamErrorUserDeclined = -3801, TCC 거부 시 대부분 이 코드
        if ns.code == -3801 || ns.localizedDescription.lowercased().contains("declined") || ns.localizedDescription.contains("TCC") {
            log("ERR_PERMISSION")
            exit(2)
        }
        log("ERR \(ns.domain) \(ns.code) \(ns.localizedDescription)")
        exit(1)
    }
}
RunLoop.main.run()
