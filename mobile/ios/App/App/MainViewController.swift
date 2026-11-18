import UIKit
import WebKit
import Capacitor

/// The app's one screen: Capacitor's web view showing orenth.app, plus the
/// few things a web page cannot do for itself.
///
/// - Registers WebAuthPlugin, the sign-in sheet (see WebAuthPlugin.swift),
///   AppPurchasePlugin, Apple's in-app purchase for Pro (see
///   AppPurchasePlugin.swift), and NavigationGuardPlugin, which keeps the web
///   view on orenth.app (see NavigationGuardPlugin.swift).
/// - A load that was only cut short is not shown as "No connection" (see
///   CancelledLoadFilter, below).
/// - Swiping in from the left edge goes back a page, as in Safari.
/// - Pulling the page down reloads it, and the spinner stops when the page
///   has finished loading.
/// - Before the first page arrives, the screen is the site's own ground,
///   light or dark to match the phone.
///
/// SceneDelegate.swift creates this screen (Capacitor 8.5 and later build the
/// window in code), and Main.storyboard names it too.
class MainViewController: CAPBridgeViewController {
    private var refresher: UIRefreshControl?
    // Ends the observation by itself when this screen goes away.
    private var loadingObservation: NSKeyValueObservation?
    // Kept here as well as in the bridge: a pull to refresh on the offline
    // page asks it which page to open again.
    private let navigationGuard: NavigationGuardPlugin = NavigationGuardPlugin()
    // The web view holds its navigation delegate weakly, so this keeps it.
    private var cancelledLoadFilter: CancelledLoadFilter?

    // Called once, after Capacitor has made the web view and the bridge and
    // before the first page loads.
    override func capacitorDidLoad() {
        super.capacitorDidLoad()
        bridge?.registerPluginInstance(WebAuthPlugin())
        // Here, at launch, so it hears StoreKit's transactions from the start,
        // as Apple asks; it keeps them until a page is listening.
        bridge?.registerPluginInstance(AppPurchasePlugin())
        // Before the first page loads, so no page from another site is ever
        // shown in the app, where it would have the whole bridge.
        bridge?.registerPluginInstance(navigationGuard)

        guard let webView: WKWebView = self.webView else {
            return
        }
        webView.allowsBackForwardNavigationGestures = true

        // Capacitor sets its own navigation delegate before this runs
        // (CAPBridgeViewController.loadView), and never sets it again, so it
        // is wrapped here once, before the first page loads.
        if let capacitorDelegate: WKNavigationDelegate = webView.navigationDelegate {
            let filter: CancelledLoadFilter = CancelledLoadFilter(wrapping: capacitorDelegate)
            cancelledLoadFilter = filter
            webView.navigationDelegate = filter
        }

        // The site's paper colour, or its dark ground in dark mode.
        let ground: UIColor = UIColor(dynamicProvider: { (traits: UITraitCollection) -> UIColor in
            if traits.userInterfaceStyle == .dark {
                return UIColor(red: 25.0 / 255.0, green: 24.0 / 255.0, blue: 23.0 / 255.0, alpha: 1.0)
            }
            return UIColor(red: 244.0 / 255.0, green: 243.0 / 255.0, blue: 238.0 / 255.0, alpha: 1.0)
        })
        webView.backgroundColor = ground
        webView.scrollView.backgroundColor = ground

        // Capacitor turns the bounce off; a pull to refresh needs it.
        let scrollView: UIScrollView = webView.scrollView
        scrollView.bounces = true
        scrollView.alwaysBounceVertical = true

        let control: UIRefreshControl = UIRefreshControl()
        control.addTarget(self, action: #selector(pulledToRefresh(_:)), for: .valueChanged)
        scrollView.refreshControl = control
        refresher = control

        // The spinner stops when the page stops loading, whether it loaded,
        // failed, or was replaced by the offline page.
        loadingObservation = webView.observe(\.isLoading, options: [.new]) { [weak self] (observed: WKWebView, change: NSKeyValueObservedChange<Bool>) in
            let stillLoading: Bool = change.newValue ?? false
            if stillLoading {
                return
            }
            guard let screen: MainViewController = self else {
                return
            }
            DispatchQueue.main.async {
                screen.stopRefreshing()
            }
        }
    }

    @objc private func pulledToRefresh(_ sender: UIRefreshControl) {
        guard let webView: WKWebView = self.webView else {
            sender.endRefreshing()
            return
        }
        if OrenthSite.isSitePage(webView.url) {
            _ = webView.reload()
            return
        }
        // On the offline page, "again" means the page that could not be
        // opened, or else the site; never the offline page itself.
        if let again: URL = navigationGuard.pageToRetry() ?? bridge?.config.serverURL {
            _ = webView.load(URLRequest(url: again))
        } else {
            _ = webView.reload()
        }
    }

    private func stopRefreshing() {
        if let control: UIRefreshControl = refresher, control.isRefreshing {
            control.endRefreshing()
        }
    }
}

/// Capacitor's own navigation delegate, with one change: a load that was
/// only cut short is not reported to it as a failure.
///
/// Capacitor 8.6.2 shows the offline page (server.errorPath) for every failed
/// load, a cancelled one too (WebViewDelegationHandler.swift, didFail and
/// didFailProvisionalNavigation). WebKit cancels a load whenever another
/// replaces it: a second tap while a slow page loads, a swipe back, a pull to
/// refresh, an app link or a tapped notification arriving while the first
/// page loads. So a phone that was online showed "No connection", and
/// opening that page cut short the page the person had asked for. Those
/// errors are dropped here. Every other failure, and every other message,
/// goes to Capacitor's delegate unchanged.
///
/// Installed by MainViewController.capacitorDidLoad().
final class CancelledLoadFilter: NSObject {
    private let capacitorDelegate: WKNavigationDelegate

    init(wrapping capacitorDelegate: WKNavigationDelegate) {
        self.capacitorDelegate = capacitorDelegate
        super.init()
    }

    // WebKit asks which messages a delegate answers once, when it is set.
    // This one answers its own two, and everything Capacitor's delegate
    // answers, which goes straight on to that delegate.
    override func responds(to aSelector: Selector!) -> Bool {
        return super.responds(to: aSelector) || capacitorDelegate.responds(to: aSelector)
    }

    override func forwardingTarget(for aSelector: Selector!) -> Any? {
        if capacitorDelegate.responds(to: aSelector) {
            return capacitorDelegate
        }
        return super.forwardingTarget(for: aSelector)
    }

    /// A load cut short rather than failed: NSURLErrorCancelled (-999), when
    /// another load replaced it, or WebKit's "Frame load interrupted" (102),
    /// when the app itself turned it away, as NavigationGuardPlugin does with
    /// a redirect to another site, which it opens outside the app instead.
    static func wasCutShort(_ error: Error) -> Bool {
        let nsError: NSError = error as NSError
        if nsError.domain == NSURLErrorDomain && nsError.code == NSURLErrorCancelled {
            return true
        }
        return nsError.domain == "WebKitErrorDomain" && nsError.code == 102
    }
}

extension CancelledLoadFilter: WKNavigationDelegate {
    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        if CancelledLoadFilter.wasCutShort(error) {
            return
        }
        capacitorDelegate.webView?(webView, didFailProvisionalNavigation: navigation, withError: error)
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        if CancelledLoadFilter.wasCutShort(error) {
            return
        }
        capacitorDelegate.webView?(webView, didFail: navigation, withError: error)
    }
}
