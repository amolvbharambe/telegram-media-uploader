import AppKit
import Foundation

// Generates an AppIcon.iconset directory with all required macOS icon resolutions
let outDir = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "AppIcon.iconset"
let fm = FileManager.default
try? fm.createDirectory(atPath: outDir, withIntermediateDirectories: true)

func renderIcon(size: Int, scale: Int, filename: String) {
    let pixelSize = size * scale
    let rep = NSBitmapImageRep(
        bitmapDataPlanes: nil,
        pixelsWide: pixelSize,
        pixelsHigh: pixelSize,
        bitsPerSample: 8,
        samplesPerPixel: 4,
        hasAlpha: true,
        isPlanar: false,
        colorSpaceName: .deviceRGB,
        bytesPerRow: 0,
        bitsPerPixel: 0
    )!
    rep.size = NSSize(width: pixelSize, height: pixelSize)

    NSGraphicsContext.saveGraphicsState()
    let ctx = NSGraphicsContext(bitmapImageRep: rep)!
    NSGraphicsContext.current = ctx
    let cg = ctx.cgContext

    let s = CGFloat(pixelSize)
    let inset = s * 0.08
    let rect = CGRect(x: inset, y: inset, width: s - inset * 2, height: s - inset * 2)
    let radius = rect.width * 0.225

    // Rounded macOS squircle background
    let bgPath = CGPath(roundedRect: rect, cornerWidth: radius, cornerHeight: radius, transform: nil)
    cg.saveGState()
    cg.addPath(bgPath)
    cg.clip()

    let colorSpace = CGColorSpaceCreateDeviceRGB()
    let colors = [
        CGColor(red: 0.06, green: 0.11, blue: 0.22, alpha: 1.0),
        CGColor(red: 0.04, green: 0.07, blue: 0.14, alpha: 1.0)
    ] as CFArray
    let grad = CGGradient(colorsSpace: colorSpace, colors: colors, locations: [0.0, 1.0])!
    cg.drawLinearGradient(grad, start: CGPoint(x: 0, y: s), end: CGPoint(x: s, y: 0), options: [])

    // Glowing Telegram-blue inner ring
    let circleDiameter = rect.width * 0.66
    let circleOrigin = CGPoint(x: (s - circleDiameter) / 2, y: (s - circleDiameter) / 2)
    let circleRect = CGRect(origin: circleOrigin, size: CGSize(width: circleDiameter, height: circleDiameter))
    let tgGradColors = [
        CGColor(red: 0.22, green: 0.72, blue: 0.96, alpha: 1.0),
        CGColor(red: 0.12, green: 0.52, blue: 0.86, alpha: 1.0)
    ] as CFArray
    let tgGrad = CGGradient(colorsSpace: colorSpace, colors: tgGradColors, locations: [0.0, 1.0])!

    cg.saveGState()
    cg.addEllipse(in: circleRect)
    cg.clip()
    cg.drawLinearGradient(tgGrad, start: CGPoint(x: s / 2, y: circleRect.maxY), end: CGPoint(x: s / 2, y: circleRect.minY), options: [])
    cg.restoreGState()

    // Upward upload arrow in crisp white
    cg.setStrokeColor(CGColor(red: 1.0, green: 1.0, blue: 1.0, alpha: 1.0))
    cg.setLineCap(.round)
    cg.setLineJoin(.round)
    cg.setLineWidth(max(2.0, s * 0.075))

    let cx = s * 0.5
    let arrowBottom = s * 0.34
    let arrowTop = s * 0.66
    let wing = s * 0.15

    cg.beginPath()
    cg.move(to: CGPoint(x: cx, y: arrowBottom))
    cg.addLine(to: CGPoint(x: cx, y: arrowTop))
    cg.move(to: CGPoint(x: cx - wing, y: arrowTop - wing * 0.95))
    cg.addLine(to: CGPoint(x: cx, y: arrowTop))
    cg.addLine(to: CGPoint(x: cx + wing, y: arrowTop - wing * 0.95))
    cg.strokePath()

    cg.restoreGState()
    NSGraphicsContext.restoreGraphicsState()

    let pngData = rep.representation(using: .png, properties: [:])!
    let dest = URL(fileURLWithPath: outDir).appendingPathComponent(filename)
    try! pngData.write(to: dest)
}

let specs: [(Int, Int, String)] = [
    (16, 1, "icon_16x16.png"),
    (16, 2, "icon_16x16@2x.png"),
    (32, 1, "icon_32x32.png"),
    (32, 2, "icon_32x32@2x.png"),
    (128, 1, "icon_128x128.png"),
    (128, 2, "icon_128x128@2x.png"),
    (256, 1, "icon_256x256.png"),
    (256, 2, "icon_256x256@2x.png"),
    (512, 1, "icon_512x512.png"),
    (512, 2, "icon_512x512@2x.png")
]

for (sz, sc, name) in specs {
    renderIcon(size: sz, scale: sc, filename: name)
}
print("Generated iconset in \(outDir)")
