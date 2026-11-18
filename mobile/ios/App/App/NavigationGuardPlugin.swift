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
/// - An https address on orenth.app itself, and the app's own offline page
///   (server.errorPath, capacitor://localhost/offline.html), are left to
///   Capacitor, which opens them in the app as before.
/// - Anything else is never opened in the app. It opens outside it: another
///   site in Safari, mailto: in Mail, and so on, as Capacitor already does
///   for another site.
///
/// A frame inside a page is left to Capacitor: the bridge is only ever put
/// into the page itself, never into its frames.
///
/// A pull to refresh on a page that answered a form would send the form
/// again (WebKit reloads a POST as a POST, and marks it .formResubmitted for
/// the app to ask about; Safari asks, Capacitor does not). A code typed
/// wrong would use up another of its tries. So that reload is cancelled, and
/// the page stays as it is. Only the reload a pull asked for: when the app
/// reloads a page by itself after iOS stopped its web content, cancelling
/// would leave the screen blank.
///
/// It also remembers the last page of the site the web view set out to
/// open, so that the offline page's Try again (and a pull to refresh there)
/// opens the page that could not be opened, not the front page. From the
/// offline page:
///   Capacitor.nativePromise('NavigationGuard', 'lastPage')
///       -> { url: 'https://orenth.app/...' }, or { url: '' }
///
/// Capacitor 8.6.2 asks each plugin's shouldOverrideLoad before its own rules
/// (WebViewDelegationHandler.webView(_:decidePolicyFor:decisionHandler:)):
/// true cancels the load, false allows it, nil leaves it to Capacitor.
/// Registered by MainViewController.capacitorDidLoad().
@objc(NavigationGuardPlugin)
public class NavigationGuardPlugin: CAPPlugin, CAPBridgedPlugin {
    public let identifier: String = "NavigationGuardPlugin"
    public let jsName: String = "NavigationGuard"
    public let pluginMethods: [CAPPluginMethod] = [
        CAPPluginMethod(name: "lastPage", returnType: CAPPluginReturnPromise)
    ]

    // The last page of the site the web view set out to open with a plain
    // GET. A form that could not be sent is not kept: trying again opens the
    // page the form was on, and never sends it a second time by itself.
    // Only read and written on the main thread.
    private var lastPageAddress: URL?

    // Set just before a pull to refresh reloads, and used up by the next
    // page the web view sets out to open, which is that reload. Only read
    // and written on the main thread.
    private var pullPending: Bool = false

    /// MainViewController calls this just before it reloads for a pull to
    /// refresh. Main thread only.
    func pullToRefreshStarting() {
        pullPending = true
    }

    // Called by Capacitor's navigation delegate, on the main thread.
    override public func shouldOverrideLoad(_ navigationAction: WKNavigationAction) -> NSNumber? {
        // The page itself: the main frame, or a new window.
        let mainFrame: Bool = navigationAction.targetFrame?.isMainFrame == true
        let wholePage: Bool = navigationAction.targetFrame == nil || mainFrame
        guard wholePage, let url: URL = navigationAction.request.url else {
            return nil
        }
        let afterPull: Bool = mainFrame && pullPending
        if mainFrame {
            pullPending = false
        }
        if OrenthSite.isSitePage(url) {
            if afterPull && navigationAction.navigationType == .formResubmitted {
                stopRefreshSpinner()
                return NSNumber(value: true)
            }
            let method: String = navigationAction.request.httpMethod ?? "GET"
            if mainFrame && method.uppercased() == "GET" {
                lastPageAddress = url
            }
            return nil
        }
        if isOfflinePage(url) {
            return nil
        }
        openOutside(url)
        return NSNumber(value: true)
    }

    /// The page to open again after a failed load, if the web view has set
    /// out to open one. Main thread only.
    func pageToRetry() -> URL? {
        return lastPageAddress
    }

    @objc func lastPage(_ call: CAPPluginCall) {
        // Plugin methods run on Capacitor's own queue.
        DispatchQueue.main.async { [weak self] in
            let address: String = self?.lastPageAddress?.absoluteString ?? ""
            call.resolve(["url": address])
        }
    }

    /// The page Capacitor shows from inside the app when a load fails
    /// (server.errorPath): the only one of the app's own files that is ever
    /// a page.
    private func isOfflinePage(_ url: URL) -> Bool {
        guard let page: URL = bridge?.config.errorPathURL,
              let pageScheme: String = page.scheme,
              let pageHost: String = page.host,
              let scheme: String = url.scheme,
              let host: String = url.host else {
            return false
        }
        return scheme.lowercased() == pageScheme.lowercased()
            && host.lowercased() == pageHost.lowercased()
            && url.path == page.path
    }

    /// The pull to refresh spinner, which a cancelled reload would leave
    /// turning: nothing loads, so nothing else stops it.
    private func stopRefreshSpinner() {
        DispatchQueue.main.async { [weak self] in
            self?.webView?.scrollView.refreshControl?.endRefreshing()
        }
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
