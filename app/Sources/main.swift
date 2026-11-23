import AppKit

let app = NSApplication.shared
let delegate = App()
if uitest || unittest { sweepTestDomains() }
#if GREENLIGHT_TESTS
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
#else
if uitest {
    app.delegate = delegate
    app.setActivationPolicy(.accessory)
} else {
    app.delegate = delegate
    app.setActivationPolicy(.regular)
}
#endif
app.run()
