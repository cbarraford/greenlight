import AppKit
import AVFoundation
import Carbon
import Combine
import CoreAudio
import CryptoKit
import Network
import Security
import Speech
import SwiftUI
import UniformTypeIdentifiers
import UserNotifications
import WebKit

let app = NSApplication.shared
let delegate = App()
if CommandLine.arguments.contains("--vimtest") { vimTest() }
if unittest {
    app.setActivationPolicy(.accessory)
    unitTest()
}
if selftest {
    CommandLine.arguments.contains("--attachtest") ? attachOnly() : selfTest()
    app.setActivationPolicy(.accessory)
} else if uitest {
    app.delegate = delegate
    app.setActivationPolicy(.accessory)
} else {
    app.delegate = delegate
    app.setActivationPolicy(.regular)
}
app.run()
