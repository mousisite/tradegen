import Foundation
import UIKit
import WebKit
import Capacitor

/// The one site the app shows, and how to tell an address on it from one
/// that only looks like it. Used by every plugin of the app's own.
enum OrenthSite {
    /// The site's host, exactly. Capacitor's own test is that an address
    /// starts with "https://orenth.app", which https://orenth.app.evil.example
    /// passes too.
    static let host: String = "orenth.app"

    /// The app's own address scheme, as Info.plist registers it.
    static let appScheme: String = "app.orenth.ios"

    /// An https address on orenth.app itself, on the usual port.
    static func isSitePage(_ url: URL?) -> Bool {
        guard let address: URL = url,
              let scheme: String = address.scheme,
              let host: String = address.host else {
            return false
        }
        let port: Int = address.port ?? 443
        return scheme.lowercased() == "https" && host.lowercased() == OrenthSite.host && port == 443
    }

    /// Whether the web view is showing one of the site's own pages: both the
    /// address it is on, which changes as soon as another page is asked for,
    /// and the page it last opened (the current entry of its history). Only
    /// on the main thread, like everything about a web view.
    static func showsSitePage(_ webView: WKWebView?) -> Bool {
        guard let view: WKWebView = webView else {
            return false
        }
        return isSitePage(view.url) && isSitePage(view.backForwardList.currentItem?.url)
    }

    /// A sign-in address of the site, https://orenth.app/auth/..., and not one
    /// that a browser would read as another of its pages ("/auth/../account").
    static func isSignInAddress(_ url: URL) -> Bool {
        guard isSitePage(url) else {
            return false
        }
        // The path with its %-escapes decoded, so "%2e%2e" shows as "..".
        let path: String = url.path
        if !path.hasPrefix("/auth/") || path.contains("\\") {
            return false
        }
        let segments: [Substring] = path.split(separator: "/")
        return !segments.contains(where: { (segment: Substring) -> Bool in
            segment == "." || segment == ".."
        })
    }
}

/// Keeps the app's web view on orenth.app.
///
/// Capacitor gives every page it shows the whole bridge to the phone: the
/// sign-in sheets, in-app purchase, notifications. It shows a page in the
/// app whenever the address starts with "https://orenth.app", and that is
/// also true of https://orenth.app.evil.example. So every address the web
/// view is about to open as a page is checked here first, before Capacitor:
///
/// - An https address on orenth.app itself, and the app's own files
///   (capacitor://localhost, where the offline page is), are left to
///   Capacitor, which opens them in the app as before.
/// - Anything else is never opened in the app. It opens outside it: another
///   site in Safari, mailto: in Mail, and so on, as Capacitor already does
///   for another site.
///
/// A frame inside a page is left to Capacitor: the bridge is only ever put
/// into the page itself, never into its frames.
///
/// Capacitor 8.6.2 asks each plugin's shouldOverrideLoad before its own rules
/// (WebViewDelegationHandler.webView(_:decidePolicyFor:decisionHandler:)):
/// true cancels the load, false allows it, nil leaves it to Capacitor.
/// Registered by MainViewController.capacitorDidLoad().
@objc(NavigationGuardPlugin)
public class NavigationGuardPlugin: CAPPlugin, CAPBridgedPlugin {
    public let identifier: String = "NavigationGuardPlugin"
    public let jsName: String = "NavigationGuard"
    public let pluginMethods: [CAPPluginMethod] = []

    // Called by Capacitor's navigation delegate, on the main thread.
    override public func shouldOverrideLoad(_ navigationAction: WKNavigationAction) -> NSNumber? {
        // The page itself: the main frame, or a new window.
        let wholePage: Bool = navigationAction.targetFrame == nil
            || navigationAction.targetFrame?.isMainFrame == true
        guard wholePage, let url: URL = navigationAction.request.url else {
            return nil
        }
        if OrenthSite.isSitePage(url) || isAppFile(url) {
            return nil
        }
        openOutside(url)
        return NSNumber(value: true)
    }

    /// capacitor://localhost: the files inside the app, where the offline
    /// page is.
    private func isAppFile(_ url: URL) -> Bool {
        guard let local: URL = bridge?.config.localURL,
              let localScheme: String = local.scheme,
              let localHost: String = local.host,
              let scheme: String = url.scheme,
              let host: String = url.host else {
            return false
        }
        return scheme.lowercased() == localScheme.lowercased()
            && host.lowercased() == localHost.lowercased()
    }

    /// Safari, or the app the address belongs to. Only while someone is using
    /// the app, as Capacitor does: a page that moves on by itself while the
    /// app is in the background opens nothing.
    private func openOutside(_ url: URL) {
        DispatchQueue.main.async { [weak self] in
            guard let scene: UIWindowScene = self?.webView?.window?.windowScene,
                  scene.activationState == .foregroundActive else {
                return
            }
            UIApplication.shared.open(url, options: [:], completionHandler: nil)
        }
    }
}
