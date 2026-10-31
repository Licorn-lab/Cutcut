import Foundation
import CoreML
import CoreVideo
import ImageIO
import CoreGraphics

func emit(_ object: Any) throws {
    let data = try JSONSerialization.data(withJSONObject: object, options: [.sortedKeys])
    print(String(decoding: data, as: UTF8.self))
    fflush(stdout)
}
func load(_ path: String) throws -> MLModel {
    let config = MLModelConfiguration(); config.computeUnits = .cpuOnly
    return try MLModel(contentsOf: URL(fileURLWithPath: path), configuration: config)
}
func vector(_ model: MLModel, _ input: MLFeatureValue) throws -> [Double] {
    let name = model.modelDescription.inputDescriptionsByName.keys.first!
    let result = try model.prediction(from: MLDictionaryFeatureProvider(dictionary: [name: input]))
    guard let array = result.featureValue(for: "final_emb_1")?.multiArrayValue else {
        throw NSError(domain: "WhiteFlow", code: 1, userInfo: [NSLocalizedDescriptionKey: "模型没有输出 final_emb_1"])
    }
    return (0..<array.count).map { array[$0].doubleValue }
}
func pixels(_ path: String) throws -> CVPixelBuffer {
    guard let src = CGImageSourceCreateWithURL(URL(fileURLWithPath: path) as CFURL, nil),
          let image = CGImageSourceCreateImageAtIndex(src, 0, nil), image.width == 256, image.height == 256 else {
        throw NSError(domain: "WhiteFlow", code: 2, userInfo: [NSLocalizedDescriptionKey: "需要256像素RGB缩略图"])
    }
    var optional: CVPixelBuffer?
    let status = CVPixelBufferCreate(kCFAllocatorDefault, 256, 256, kCVPixelFormatType_32ARGB,
                                    [kCVPixelBufferCGImageCompatibilityKey: true, kCVPixelBufferCGBitmapContextCompatibilityKey: true] as CFDictionary, &optional)
    guard status == kCVReturnSuccess, let buffer = optional else { throw NSError(domain: "WhiteFlow", code: 3) }
    CVPixelBufferLockBaseAddress(buffer, [])
    defer { CVPixelBufferUnlockBaseAddress(buffer, []) }
    guard let ctx = CGContext(data: CVPixelBufferGetBaseAddress(buffer), width: 256, height: 256,
                              bitsPerComponent: 8, bytesPerRow: CVPixelBufferGetBytesPerRow(buffer),
                              space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.noneSkipFirst.rawValue) else { throw NSError(domain: "WhiteFlow", code: 4) }
    ctx.draw(image, in: CGRect(x: 0, y: 0, width: 256, height: 256))
    return buffer
}
do {
    let args = CommandLine.arguments
    guard args.count >= 3 else { throw NSError(domain: "WhiteFlow", code: 5) }
    if args[1] == "compile" {
        let compiled = try MLModel.compileModel(at: URL(fileURLWithPath: args[2]))
        try FileManager.default.moveItem(at: compiled, to: URL(fileURLWithPath: args[3]))
        try emit(["compiled": args[3]])
    } else if args[1] == "text" {
        let model = try load(args[2]); let tokenizer = CLIPTokenizer(resources: URL(fileURLWithPath: args[3]))
        let prompts = try JSONDecoder().decode([String].self, from: Data(contentsOf: URL(fileURLWithPath: args[4])))
        var vectors: [[Double]] = []
        for prompt in prompts {
            let array = try MLMultiArray(shape: [1,77], dataType: .int32)
            for (i,t) in tokenizer.encode_full(text: prompt).enumerated() { array[i] = NSNumber(value: t) }
            vectors.append(try vector(model, MLFeatureValue(multiArray: array)))
        }
        try emit(vectors)
    } else if args[1] == "image" {
        let start = Date(); let model = try load(args[2]); let loadSeconds = Date().timeIntervalSince(start)
        for path in args.dropFirst(3) {
            try autoreleasepool {
                let tick = Date(); let input = try pixels(path)
                let embedding = try vector(model, MLFeatureValue(pixelBuffer: input))
                try emit(["path": path, "embedding": embedding, "seconds": Date().timeIntervalSince(tick), "load_seconds": loadSeconds])
            }
        }
    } else { throw NSError(domain: "WhiteFlow", code: 6) }
} catch {
    fputs("\(error)\n", stderr); exit(1)
}
