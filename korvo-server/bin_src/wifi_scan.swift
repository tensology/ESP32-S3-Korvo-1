#!/usr/bin/env swift
import CoreWLAN
import Foundation

let client = CWWiFiClient.shared()
guard let wifiInterface = client.interface(withName: "en0") else {
    print("ERROR: No WiFi interface")
    exit(0)
}

// Set a timeout: if scan doesn't return in 3 seconds, exit
let timer = Timer.scheduledTimer(withTimeInterval: 3.0, repeats: false) { _ in
    exit(0)
}
RunLoop.current.add(timer, forMode: .default)

do {
    let networks = try wifiInterface.scanForNetworks(withName: nil as String?)
    for net in networks {
        if let ssid = net.ssid, !ssid.isEmpty {
            let rssi = net.rssiValue
            print("\(ssid)|\(rssi)|Unknown")
        }
    }
} catch {
    // Silent fail — macOS probably denied location access
}

// Exit immediately
exit(0)